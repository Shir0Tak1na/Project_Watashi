#!/usr/bin/env python3
"""Real time correction verification. Headless: no screen, no models.

The feature under test is the loop a user actually performs: a translation comes out
wrong, they type the right one, and the screen changes. Four things have to happen for
that to be true, and each of them fails silently on its own:

1. the correction is written to a **file** in the user corpus layer, atomically, without
   touching any hand written corpus file;
2. the engine **reloads** it -- which it did not do at all before this check existed:
   ``reload_if_changed`` was implemented, documented, and called from nowhere, so
   every "hot reload" claim in the docs was false;
3. the **reuse memory** is cleared for that text, because otherwise the rejected
   translation is served from cache for the next ten seconds and the fix is invisible;
4. the frame **on screen** is repainted, because a still screen produces no new frame,
   so the correction would otherwise sit in the corpus and never be shown.

The negative controls matter as much as the assertions: a correction that is *not*
applied to an unrelated sentence, a corpus edit that is *not* picked up with hot reload
off, a short line that is *not* matched loosely, and a translation that is unchanged
before the correction is recorded.

    run.cmd selfcheck_correct --summary
"""

from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker  # noqa: E402

from watashi import correct as corrections
from watashi.config import AppConfig
from watashi.events import (
    CMD_CORRECT,
    CMD_LIST_CORRECTIONS,
    CMD_REMOVE_CORRECTION,
    CMD_SET_CORPUS_RELOAD,
    EVENT_CORRECTION,
    EVENT_SUBTITLE,
    OverlayUpdate,
    TranslatedLine,
)
from watashi.local_nmt import (
    MIN_RESIDUAL_WORDS,
    HybridTranslator,
    protect_terms,
    refine_with_nmt,
    residual_word_count,
)
from watashi.pipeline import Pipeline, PipelineConfig
from watashi.recent import MIN_KEY_LENGTH, RecentTranslations
from watashi.session import Session, build_corpus
from watashi.translate import LAYER_DOMAIN, LAYER_GENERAL, LAYER_USER, CorpusStore, CorpusTranslator

#: the sentence used throughout: wrong in the domain corpus, right after a correction
BAD_LINE = "他突破到了虚空境界"
BAD_TRANSLATION = "He broke through to the void realm"
GOOD_TRANSLATION = "He has broken through into the Void Realm"
UNRELATED = "剑意是这宗门的根本"


def write_corpus(directory: Path, name: str, entries: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return path


def build_store(tmp: Path, *, interval_s: float = 0.0, auto_reload: bool = True) -> CorpusStore:
    """A three layer corpus in a scratch directory, with no real project files."""
    return CorpusStore(
        layers={
            LAYER_USER: [tmp / "user"],
            LAYER_DOMAIN: [tmp / "domain"],
            LAYER_GENERAL: [tmp / "general"],
        },
        rule_files=[],
        auto_reload=auto_reload,
        reload_interval_s=interval_s,
    )


class _FakeModel:
    """Stands in for the local model.

    No model is loaded: the questions here are whether it is consulted at all about a
    sentence a human has already settled, and whether an answer it computed *before*
    that correction can reach the screen afterwards. Both are about ordering, not about
    translation quality.

    ``translate_batch`` is the method the batched refinement path really uses;
    ``translate_one`` is the single-sentence path. Implementing both, and counting
    them separately, is what stops the checks from passing because the fake was never
    on the code path being tested -- which is how the first version of this test
    "passed" while never reaching the model.
    """

    available = True
    load_error = None
    placeholder_scheme = "≤{i}≥"
    compute_type = "int8"

    def __init__(self, answer: str = "a model answer", block: bool = False) -> None:
        self.answer = answer
        self.block = block
        self.single_calls = 0
        self.batch_calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.stats = SimpleNamespace(
            sentences=0, errors=0, mean_ms=0.0, protected_terms=0, lost_placeholders=0
        )

    @property
    def calls(self) -> int:
        return self.single_calls + self.batch_calls

    def translate_one(self, text: str, target: str, source: str | None = None) -> str:
        self.single_calls += 1
        self.entered.set()
        if self.block:
            self.release.wait(timeout=10.0)
        self.finished.set()
        return self.answer

    def translate_batch(
        self, texts: list[str], target: str, source: str | None = None
    ) -> list[str]:
        self.batch_calls += 1
        self.entered.set()
        if self.block:
            self.release.wait(timeout=10.0)
        self.finished.set()
        return [self.answer for _ in texts]


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("Real time correction self check (no screen, no models)")
    print("=" * 78)

    # ---------------------------------------------------------------- #
    check.section("the corrections file: written, readable, and not in the way")

    root = Path(tempfile.mkdtemp(prefix="watashi-correct-"))
    path = root / "user" / corrections.CORRECTIONS_FILE
    store = corrections.Corrections(path)

    check.check("a missing file is not an error", len(store) == 0)
    check.check("and nothing is matched", store.lookup_line(BAD_LINE) is None)

    record, created = store.record(BAD_LINE, GOOD_TRANSLATION)
    check.check("recording reports it as new", created and record.count == 1)
    check.check("the file now exists", path.exists(), str(path))
    check.check("no temp file is left behind", not list(path.parent.glob("*.tmp")))

    payload = json.loads(path.read_text(encoding="utf-8"))
    check.check(
        "the file is the shape the corpus loader reads",
        isinstance(payload.get("entries"), dict) and BAD_LINE in payload["entries"],
        f"keys={sorted(payload)}",
    )
    check.check(
        "the entry carries its target", payload["entries"][BAD_LINE]["target"] == GOOD_TRANSLATION
    )
    check.check(
        "and the scope it was made with", payload["entries"][BAD_LINE]["scope"] == "line"
    )
    check.check("with a note saying who wrote it", "_readme" in payload)

    reread = corrections.Corrections(path)
    check.check(
        "a fresh reader sees the correction: it survives a restart",
        reread.lookup_line(BAD_LINE) is not None,
        f"{len(reread)} entry(ies)",
    )

    check.check(
        "the author's source is not rewritten",
        reread.lookup_exact(BAD_LINE).source == BAD_LINE,
    )

    # ---------------------------------------------------------------- #
    check.section("correcting the same line again updates it instead of forking")

    again, created_again = reread.record(BAD_LINE, "Another reading entirely")
    check.check("a second correction is an update", not created_again)
    check.check("the count records how often it was corrected", again.count == 2, f"{again.count}")
    check.check("the latest answer wins", reread.lookup_line(BAD_LINE).target == "Another reading entirely")
    check.check(
        "and there is still one row for one sentence",
        len(reread) == 1,
        f"{len(reread)} row(s), not two rival answers for one line",
    )

    # ---------------------------------------------------------------- #
    check.section("loose matching: the point of a line correction")

    loose = corrections.Corrections(path)
    loose.record(BAD_LINE, GOOD_TRANSLATION)
    variants = [
        BAD_LINE,
        BAD_LINE + "。",
        BAD_LINE + "！",
        " " + BAD_LINE + " ",
        BAD_LINE + "…",
    ]
    hits = [loose.lookup_line(v) for v in variants]
    check.check(
        "every punctuation variant OCR might produce is matched",
        all(h is not None for h in hits),
        f"{sum(1 for h in hits if h)}/{len(variants)} matched",
    )
    check.check(
        "and they all return the corrected translation",
        all(h.target == GOOD_TRANSLATION for h in hits if h),
    )
    check.check("a lookup is counted, so 'is it working' has an answer", hits[0].hits >= 1)
    check.check(
        "an unrelated sentence is not matched",
        loose.lookup_line(UNRELATED) is None,
        "a correction that leaks onto other lines would be worse than none",
    )
    check.check(
        "a short line is stored but not matched loosely",
        (lambda: (
            loose.record("好的", "OK"),
            loose.lookup_line("好的。") is None,
            loose.lookup_exact("好的") is not None,
        )[-1])(),
        f"below {MIN_KEY_LENGTH} characters the loose key collides with other lines",
    )

    # ---------------------------------------------------------------- #
    check.section("scope: a term correction is not a line correction")

    scoped = corrections.Corrections(root / "user" / "scoped.json")
    scoped.record("虚空境界", "Void Realm", scope=corrections.SCOPE_TERM)
    check.check(
        "a term correction is not matched as a whole line",
        scoped.lookup_line("虚空境界") is None,
        "otherwise correcting one word would rewrite every line that is that word",
    )
    check.check("but it is stored", scoped.lookup_exact("虚空境界") is not None)
    check.check(
        "line scope is matched loosely, term scope exactly",
        scoped.lookup_line("虚空境界。") is None,
    )

    # ---------------------------------------------------------------- #
    check.section("editing is reversible, and a broken file is survivable")

    check.check("removing reports success", scoped.remove("虚空境界"))
    check.check("and the entry is gone", len(scoped) == 0 and scoped.lookup_exact("虚空境界") is None)
    check.check("removing something absent is reported, not silent", not scoped.remove("不存在"))

    broken = root / "user" / "broken.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    surviving = corrections.Corrections(broken)
    check.check("a hand edit with a stray comma does not take the engine down", len(surviving) == 0)
    surviving.record("测试", "test")
    check.check(
        "and recording recovers the file rather than staying broken",
        corrections.Corrections(broken).lookup_exact("测试") is not None,
    )

    # ---------------------------------------------------------------- #
    check.section("where corrections live, and what they must not touch")

    handwritten = write_corpus(root / "user", "my_own_terms.json", {"宗门": "sect"})
    before_bytes = handwritten.read_bytes()
    neighbour = corrections.Corrections(root / "user" / corrections.CORRECTIONS_FILE)
    neighbour.record("宗门", "the Sect")
    check.check(
        "a hand written corpus file beside it is byte for byte unchanged",
        handwritten.read_bytes() == before_bytes,
        "a machine written file must never rewrite the file the user maintains",
    )
    check.check(
        "corrections go to their own file",
        (root / "user" / corrections.CORRECTIONS_FILE).exists(),
    )

    counted = build_store(root)
    check.check(
        "a corpus store built from this directory counts both files",
        counted.lookup_exact("宗门") is not None,
        f"{counted.size} entries",
    )
    check.check(
        "the correction outranks the hand written entry with the same key",
        counted.lookup_exact("宗门").layer == LAYER_USER
        and counted.lookup_exact("宗门").target == "the Sect",
        f"target={counted.lookup_exact('宗门').target!r}",
    )

    existing = correction_path = corrections.resolve_corrections_path([root / "user"])
    check.check(
        "the engine and the config agree on the path",
        existing == root / "user" / corrections.CORRECTIONS_FILE,
        str(existing),
    )
    check.check(
        "a user layer configured as a single file puts corrections beside it",
        corrections.resolve_corrections_path([root / "user" / "my_own_terms.json"])
        == root / "user" / corrections.CORRECTIONS_FILE,
    )

    # ---------------------------------------------------------------- #
    check.section("live project config: the shipped user layer does not exist yet")

    project = AppConfig.load()
    resolved = corrections.corrections_dir(project)
    check.check(
        "corrections go to the layer the config names as the user corpus",
        resolved == project.corpus_dirs("user")[0],
        f"{resolved} vs {project.corpus_dirs('user')[0]}",
    )
    check.check(
        "and to their own file inside it",
        corrections.corrections_path(project).name == corrections.CORRECTIONS_FILE,
    )
    missing = not resolved.exists()
    check.check(
        "the two reload controls ship in the config",
        project.corpus.get("auto_reload") is True
        and int(project.corpus.get("reload_interval_ms")) == 500,
        f"auto_reload={project.corpus.get('auto_reload')} "
        f"interval={project.corpus.get('reload_interval_ms')}",
    )
    # Recorded here rather than asserted either way: in a fresh checkout this layer is
    # absent, which is why the writer creates it instead of assuming it exists.
    print(f"       note: the shipped user layer {'is missing' if missing else 'exists'}: {resolved}")

    fresh = root / "fresh" / "nested" / "user"
    check.check("that layer is genuinely absent to start with", not fresh.exists())
    corrections.Corrections(fresh / corrections.CORRECTIONS_FILE).record("新词", "a new word")
    check.check(
        "a user corpus layer that does not exist is created on first use",
        (fresh / corrections.CORRECTIONS_FILE).is_file(),
        str(fresh / corrections.CORRECTIONS_FILE),
    )
    check.check(
        "and the engine loads it like any other corpus file",
        CorpusStore(
            layers={LAYER_USER: [fresh], LAYER_DOMAIN: [], LAYER_GENERAL: []},
            rule_files=[],
        ).lookup_exact("新词")
        is not None,
    )

    wired = build_corpus(project)
    check.check(
        "the config's reload switch reaches the engine that build_corpus creates",
        wired.auto_reload is True,
        f"auto_reload={wired.auto_reload}",
    )
    check.check(
        "and so does the interval, converted from the milliseconds in the config",
        abs(wired.reload_interval_s - 0.5) < 1e-9,
        f"reload_interval_s={wired.reload_interval_s} from "
        f"{project.corpus.get('reload_interval_ms')} ms",
    )
    check.check(
        "the engine built from the project config reads the same corrections file",
        wired.corrections is not None
        and wired.corrections.path == corrections.corrections_path(project),
        str(wired.corrections.path if wired.corrections else None),
    )

    # ---------------------------------------------------------------- #
    check.section("hot reload actually happens (it did not before)")

    live_dir = root / "live"
    write_corpus(live_dir, "terms.json", {BAD_LINE: BAD_TRANSLATION})
    live = CorpusStore(
        layers={LAYER_USER: [live_dir], LAYER_DOMAIN: [], LAYER_GENERAL: []},
        rule_files=[],
        auto_reload=True,
        reload_interval_s=0.0,
    )
    check.check(
        "the corpus translates the line the way the file says",
        live.translate(BAD_LINE).target_text == BAD_TRANSLATION,
        live.translate(BAD_LINE).target_text,
    )

    # An edit made by something else entirely -- an editor, a git pull, the correction
    # command in another process -- with no reload call from the caller.
    write_corpus(live_dir, "terms.json", {BAD_LINE: GOOD_TRANSLATION})
    os.utime(live_dir / "terms.json", None)
    changed = live.translate(BAD_LINE).target_text
    check.check(
        "translate() picks up an edit made on disk, with no explicit reload",
        changed == GOOD_TRANSLATION,
        f"got {changed!r}: this is the assertion that failed before reload_if_changed had a caller",
    )
    check.check("and the reload is counted", live.reloads >= 1, f"reloads={live.reloads}")

    write_corpus(live_dir, "second.json", {"新的词条": "a new term"})
    live.translate(BAD_LINE)
    check.check(
        "a brand new file in the directory is picked up too",
        live.translate("新的词条").target_text == "a new term",
    )

    # The granularity blind spot, made deterministic instead of left as a flake: NTFS
    # timestamps come from a clock that ticks every ~15.6 ms, so an edit landing in the
    # same tick as the previous write has an *identical* mtime. This reproduces that
    # exactly by forcing the mtime backwards to the value it already had -- the state a
    # same-tick write leaves behind -- and asserts the edit is still seen, because the
    # snapshot records size as well as time. Before that, this file failed roughly one
    # run in ten with a reload counter of zero.
    terms = live_dir / "terms.json"
    same_tick = live_dir / "same_tick.json"
    write_corpus(live_dir, "same_tick.json", {"同时": "same tick, short"})
    live.translate(BAD_LINE)
    original_mtime = same_tick.stat().st_mtime
    write_corpus(live_dir, "same_tick.json", {"同时": "same tick, much longer translation"})
    os.utime(same_tick, (original_mtime, original_mtime))
    check.check(
        "an edit stamped with the previous write's own mtime is still detected",
        same_tick.stat().st_mtime == original_mtime
        and live.translate("同时").target_text == "same tick, much longer translation",
        f"mtime={same_tick.stat().st_mtime} original={original_mtime} "
        f"-> {live.translate('同时').target_text!r}: size is what catches this",
    )
    check.check(
        "and the reload counter saw it",
        live.reloads >= 1,
        f"reloads={live.reloads}",
    )
    check.check("the term the other file holds is still right", terms.exists())

    # ---------------------------------------------------------------- #
    check.section("the throttle, and the switches that turn reload off")

    throttled = build_store(root, interval_s=1000.0)
    check.check("the first check always runs", throttled.reload_if_changed(now=100.0) is False)
    write_corpus(root / "domain", "later.json", {"稍后": "later"})
    check.check(
        "a change inside the interval is not seen",
        throttled.reload_if_changed(now=100.1) is False,
        "the whole point of the throttle: one stat burst per interval, not per frame",
    )
    check.check(
        "forcing bypasses the interval",
        throttled.reload_if_changed(now=100.2, force=True) is True,
        "a correction the user is watching for must not wait out the throttle",
    )
    check.check("and the new entry is there", throttled.lookup_exact("稍后") is not None)

    write_corpus(root / "domain", "later2.json", {"更晚": "later still"})
    check.check(
        "once the interval has passed, a change is seen again",
        throttled.reload_if_changed(now=1100.0) is True,
    )

    off = build_store(root, auto_reload=False)
    write_corpus(root / "domain", "off.json", {"关掉": "off"})
    check.check(
        "with hot reload off, an edit is NOT picked up",
        off.reload_if_changed(now=1.0) is False and off.lookup_exact("关掉") is None,
        "the negative control: without it, 'reload happened' proves nothing",
    )
    off.translate("任意")
    check.check("and translate() does not check either", off.lookup_exact("关掉") is None)
    check.check(
        "but a forced reload still works, which is how corrections behave",
        off.reload_if_changed(now=2.0, force=True) is True
        and off.lookup_exact("关掉") is not None,
    )
    off.auto_reload = True
    check.check("the switch is live", off.auto_reload is True)

    # Changing the interval restarts the policy: one check happens straight away under
    # the new setting, then the new interval applies. Without the reset, a user who
    # lengthens the interval right after a check waits out the *new* long interval
    # before their just-made corpus edit is ever seen.
    policy = build_store(root, interval_s=0.1)
    check.check("the first check runs and arms the throttle", policy.reload_if_changed(now=100.0) is False)
    write_corpus(root / "domain", "policy.json", {"改政策": "policy"})
    check.check(
        "50 ms later, with a 100 ms interval, the change is not seen yet",
        policy.reload_if_changed(now=100.05) is False,
    )
    policy.reload_interval_s = 5.0
    check.check(
        "lengthening the interval checks once immediately rather than going quiet",
        policy.reload_if_changed(now=100.06) is True,
        "0.06 s after the last check, a 5 s interval would otherwise swallow the edit",
    )
    check.check("and the edit is applied", policy.lookup_exact("改政策") is not None)
    check.check(
        "after which the new interval is in force",
        policy.reload_if_changed(now=100.07) is False,
    )

    # ---------------------------------------------------------------- #
    check.section("the engine returns the correction, and the model leaves it alone")

    corrected_dir = root / "corrected"
    write_corpus(corrected_dir, "terms.json", {BAD_LINE: BAD_TRANSLATION, UNRELATED: "The sword intent"})
    engine = CorpusStore(
        layers={LAYER_USER: [corrected_dir], LAYER_DOMAIN: [], LAYER_GENERAL: []},
        rule_files=[],
        reload_interval_s=0.0,
    )
    check.check(
        "before the correction the domain file's translation is what comes out",
        engine.translate(BAD_LINE).target_text == BAD_TRANSLATION,
        "the control: an unchanged result after correcting would prove nothing",
    )
    check.check(
        "the correction file is empty to start with",
        engine.corrections is not None and len(engine.corrections) == 0,
    )

    engine.corrections.record(BAD_LINE, GOOD_TRANSLATION)
    engine.load()
    outcome = engine.translate(BAD_LINE)
    check.check(
        "the correction outranks the corpus entry it was made to fix",
        outcome.target_text == GOOD_TRANSLATION,
        f"got {outcome.target_text!r}",
    )
    check.check("the span says where it came from", outcome.spans[0].origin == "correction:user")
    check.check("it is fully confident", outcome.spans[0].confidence == 1.0)
    check.check("it counts as explained, not as a guess", outcome.spans[0].explained)
    check.check("so coverage is complete", outcome.coverage == 1.0, f"{outcome.coverage}")

    check.check(
        "a punctuation variant of the corrected line also gets the correction",
        engine.translate(BAD_LINE + "。").target_text == GOOD_TRANSLATION,
        "OCR will not read the same string twice, so an exact-only match is useless",
    )
    check.check(
        "a different sentence is untouched",
        engine.translate(UNRELATED).target_text == "The sword intent",
    )

    # A whole line correction masks the entire sentence, which is the documented reason
    # refine_with_nmt declines to run the model: with no residual context the model
    # emits unrelated text, and here it would overwrite a human's answer. Asserted on
    # the real function with a stand-in model that records whether it was called --
    # the arithmetic above only shows the skip *should* happen.
    protected = protect_terms(BAD_LINE, outcome, "≤{i}≥", 0.4)
    check.check(
        "a whole line correction is protected as one term",
        protected.count == 1,
        f"{protected.count} term(s)",
    )
    check.check(
        "leaving the model nothing to translate",
        residual_word_count(protected.masked, protected.pattern) < MIN_RESIDUAL_WORDS,
        f"residual={residual_word_count(protected.masked, protected.pattern)!r} "
        f"< {MIN_RESIDUAL_WORDS}",
    )

    uncorrected = engine.translate(UNRELATED)
    recorder = _FakeModel()
    refinement = refine_with_nmt(
        recorder, engine, BAD_LINE, "zh-CN", protect=True, min_confidence=0.4
    )
    check.check(
        "the model is never asked to translate a line a human corrected",
        recorder.calls == 0,
        f"the model was called {recorder.calls} time(s)",
    )
    check.check(
        "and the correction is what comes back, not the model's opinion",
        refinement.used_nmt is False
        and refinement.target_text == GOOD_TRANSLATION
        and refinement.fallback_reason == "no context left after masking terms",
        f"used_nmt={refinement.used_nmt} target={refinement.target_text!r} "
        f"reason={refinement.fallback_reason!r}",
    )

    # The control for the assertion above: the same stand-in and the same call, on a line
    # with no coverage at all, and the model *is* reached. Without it, "calls == 0" would
    # also pass if the pipeline never reached the model for an unrelated reason.
    # A line the corpus covers *in full* would not do as a control: it masks to nothing
    # and is skipped for exactly the same reason a correction is.
    uncovered = "a line with no corpus coverage whatsoever"
    control = _FakeModel()
    refine_with_nmt(control, engine, uncovered, "zh-CN", protect=True, min_confidence=0.4)
    check.check(
        "the control: with no coverage the model is called as usual",
        control.calls == 1,
        f"the model was called {control.calls} time(s)",
    )
    check.check(
        "so a corrected line is treated differently from one that is merely uncovered",
        uncorrected.target_text == "The sword intent",
        f"the corpus line translates to {uncorrected.target_text!r}",
    )

    # ---------------------------------------------------------------- #
    check.section("a refinement already in flight must not undo the correction")

    # This is the race the feature would otherwise lose silently: the model is asked to
    # improve a line, the user corrects that line while it is thinking, and the answer
    # comes back afterwards -- over the correction, and into the refinement cache, where
    # it keeps being served to later frames. The stand-in model blocks until the test
    # says so, which makes the interleaving deterministic instead of a race.
    #
    # The line has to be *partly* covered, or there is no race to test: a line the
    # corpus covers in full is never sent to the model at all.
    race_dir = root / "race"
    write_corpus(race_dir, "terms.json", {"虚空境界": "Void Realm"})
    race_line = "he broke through to 虚空境界 just now"
    race_fixed = "He has broken through into the Void Realm just now"
    race_engine = CorpusStore(
        layers={LAYER_USER: [race_dir], LAYER_DOMAIN: [], LAYER_GENERAL: []},
        rule_files=[],
        reload_interval_s=0.0,
    )
    check.check(
        "the race line is partly covered, so the model really is asked about it",
        race_engine.translate(race_line).coverage < 1.0
        and "Void Realm" in race_engine.translate(race_line).target_text,
        f"coverage={race_engine.translate(race_line).coverage}",
    )

    published: list[str] = []
    # The answer keeps the placeholder, so it is a *genuine* model answer rather than a
    # fallback: the engine rejects any model output that loses a protected term, and a
    # fallback would only be showing the pre-correction corpus text again.
    slow = _FakeModel(answer="the model's answer about ≤0≥", block=True)
    hybrid = HybridTranslator(
        corpus=race_engine,
        model=slow,
        on_refined=lambda refinement: published.append(refinement.target_text),
    )
    hybrid.start()
    try:
        hybrid.submit(race_line, "zh-CN")
        check.check(
            "the model has been handed the line and is working on it",
            slow.entered.wait(timeout=5.0) and slow.batch_calls == 1,
            f"entered={slow.entered.is_set()} batch_calls={slow.batch_calls}",
        )
        revision_before = race_engine.revision
        race_engine.corrections.record(race_line, race_fixed)
        race_engine.load()
        check.check(
            "recording a correction moves the vocabulary revision on",
            race_engine.revision > revision_before,
            f"{revision_before} -> {race_engine.revision}",
        )
        slow.release.set()
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline and not slow.finished.is_set():
            time.sleep(0.01)
        time.sleep(0.05)
    finally:
        hybrid.stop()

    check.check(
        "the model did finish and return an answer",
        slow.finished.is_set() and slow.batch_calls == 1,
        f"batch_calls={slow.batch_calls} finished={slow.finished.is_set()}",
    )
    check.check(
        "but that answer is discarded, not published over the correction",
        published == [],
        f"published {published!r}",
    )
    check.check(
        "so the line shows the correction, whichever thread finishes last",
        hybrid.translate(race_line, "zh-CN").target_text == race_fixed,
        hybrid.translate(race_line, "zh-CN").target_text,
    )
    check.check(
        "and the discard is counted rather than silent",
        hybrid.stale_dropped >= 1 and hybrid.refinements == 0,
        f"stale_dropped={hybrid.stale_dropped} refinements={hybrid.refinements}",
    )

    # The other half of the same problem: an answer that was cached *before* the
    # correction. It is not in flight any more, so only the revision can tell that it
    # describes a vocabulary that no longer exists.
    cached_dir = root / "cached"
    write_corpus(cached_dir, "terms.json", {"虚空境界": "Void Realm"})
    cached_line = "she walked into 虚空境界 yesterday"
    cached_engine = CorpusStore(
        layers={LAYER_USER: [cached_dir], LAYER_DOMAIN: [], LAYER_GENERAL: []},
        rule_files=[],
        reload_interval_s=0.0,
    )
    fast = _FakeModel(answer="the model's older answer about ≤0≥")
    cached_hybrid = HybridTranslator(corpus=cached_engine, model=fast)
    cached_hybrid.start()
    try:
        cached_hybrid.submit(cached_line, "zh-CN")
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline and cached_hybrid.refinements == 0:
            time.sleep(0.01)
        cached_answer = "the model's older answer about Void Realm"
        check.check(
            "the model's answer is cached and served back",
            cached_hybrid.translate(cached_line, "zh-CN").target_text == cached_answer,
            f"got {cached_hybrid.translate(cached_line, 'zh-CN').target_text!r}, "
            f"expected {cached_answer!r} (refinements={cached_hybrid.refinements})",
        )
        cached_engine.corrections.record(cached_line, "She walked into the Void Realm yesterday")
        cached_engine.load()
        check.check(
            "after a correction the cached pre-correction answer is not served",
            cached_hybrid.translate(cached_line, "zh-CN").target_text
            == "She walked into the Void Realm yesterday",
            f"got {cached_hybrid.translate(cached_line, 'zh-CN').target_text!r}",
        )
        check.check(
            "and nothing had to be dropped from the model path to achieve that",
            cached_hybrid.stale_dropped == 0,
            "the answer was already cached before the change, not in flight",
        )
    finally:
        cached_hybrid.stop()

    # The control: with no vocabulary change, an in-flight answer is published normally.
    # Otherwise "published == []" above could pass because the worker never ran.
    control_published: list[str] = []
    control_engine = CorpusStore(
        layers={LAYER_USER: [root / "control"], LAYER_DOMAIN: [], LAYER_GENERAL: []},
        rule_files=[],
        reload_interval_s=0.0,
    )
    quick = _FakeModel(answer="an ordinary model answer")
    control_hybrid = HybridTranslator(
        corpus=control_engine,
        model=quick,
        on_refined=lambda refinement: control_published.append(refinement.target_text),
    )
    control_hybrid.start()
    try:
        control_hybrid.submit(uncovered, "zh-CN")
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline and not control_published:
            time.sleep(0.01)
    finally:
        control_hybrid.stop()
    check.check(
        "the control: an unchanged vocabulary publishes the refinement as usual",
        control_published == ["an ordinary model answer"] and quick.batch_calls == 1,
        f"published {control_published!r} batch_calls={quick.batch_calls}",
    )

    # ---------------------------------------------------------------- #
    check.section("a term correction fixes the word and leaves the sentence alone")

    term_engine = CorpusStore(
        layers={LAYER_USER: [root / "term_user"], LAYER_DOMAIN: [], LAYER_GENERAL: []},
        rule_files=[],
        reload_interval_s=0.0,
    )
    term_engine.corrections.record("虚空境界", "Void Realm", scope=corrections.SCOPE_TERM)
    term_engine.load()
    sentence = f"他突破到了虚空境界，{UNRELATED}"
    translated = term_engine.translate(sentence).target_text
    check.check(
        "the corrected term appears in the sentence",
        "Void Realm" in translated,
        f"got {translated!r}",
    )
    check.check(
        "and the rest of the sentence is not replaced by the term's translation",
        translated != "Void Realm",
        "a term correction is not a whole line correction",
    )

    # ---------------------------------------------------------------- #
    check.section("the reuse memory is cleared, or the fix stays hidden")

    memory = RecentTranslations(ttl_s=1000.0)
    memory.put(BAD_LINE, BAD_TRANSLATION, now=0.0)
    memory.put(UNRELATED, "The sword intent", now=0.0)
    check.check("the old translation is being served from cache", memory.get(BAD_LINE, now=1.0) == BAD_TRANSLATION)
    dropped = memory.drop(BAD_LINE)
    check.check("dropping the corrected line removes exactly it", dropped == 1)
    check.check(
        "so the stale translation is no longer served",
        memory.get(BAD_LINE, now=1.0) is None,
        "without this the correction is invisible for the rest of the TTL",
    )
    check.check(
        "and an unrelated line keeps its memory",
        memory.get(UNRELATED, now=1.0) == "The sword intent",
    )
    memory.put("他突破到了虚空境界，剑意是这宗门的根本", "stale whole line", now=2.0)
    check.check(
        "a term inside a line invalidates the whole line's memory",
        memory.drop_containing("虚空境界") == 1,
        "",
    )
    check.check(
        "but not a line that does not contain it",
        memory.get(UNRELATED, now=3.0) == "The sword intent",
    )
    memory.put("", "")
    check.check("dropping nothing is not an error", memory.drop_containing("") == 0)

    # ---------------------------------------------------------------- #
    check.section("through the session boundary: one command fixes the screen")

    tmp_project = Path(tempfile.mkdtemp(prefix="watashi-correct-session-"))
    import shutil

    shutil.copy2(Path(__file__).resolve().parent / "config.yaml", tmp_project / "config.yaml")
    session_config = AppConfig.load(tmp_project / "config.yaml")
    # Point the session's own user layer at the scratch directory, so the path the
    # command writes through and the path the engine reads are the same one. They are
    # derived independently, and this is the assertion that they agree.
    session_config.corpus["user"] = ["user"]
    check.check(
        "the session's correction path and the engine's are the same file",
        build_store(tmp_project).corrections.path == corrections.corrections_path(session_config),
        f"{build_store(tmp_project).corrections.path} vs {corrections.corrections_path(session_config)}",
    )

    session_corpus = build_store(tmp_project)
    write_corpus(tmp_project / "domain", "terms.json", {BAD_LINE: BAD_TRANSLATION})
    session_corpus.load()

    session = Session(session_config, translator=CorpusTranslator(session_corpus))
    # A real pipeline object, never started: it is what owns the reuse memory, and the
    # memory is half of why a correction takes effect.
    session._pipeline = Pipeline(
        capturer=None,
        ocr=None,
        translator=session.translator,
        config=PipelineConfig(),
        on_update=None,
        on_stats=None,
    )

    frame = OverlayUpdate(
        source_text=BAD_LINE,
        target_text=BAD_TRANSLATION,
        backend="corpus+rules",
        lines=[
            TranslatedLine(
                source=BAD_LINE, target=BAD_TRANSLATION, confidence=0.95, coverage=1.0
            )
        ],
    )
    channel = session.subscribe()
    session._pipeline.recent.put(BAD_LINE, BAD_TRANSLATION)
    session._on_update(frame)
    check.check("the frame is on the session's mind", session._last_update is frame)
    check.check("and in the history", len(session.history) == 1)
    check.check(
        "the reuse memory holds the rejected translation",
        session._pipeline.recent.get(BAD_LINE, now=time.perf_counter()) == BAD_TRANSLATION,
    )
    session._pipeline.recent.put(UNRELATED, "The sword intent")

    while not channel.empty():
        channel.get_nowait()

    result = session.command(CMD_CORRECT, {"source": BAD_LINE, "target": GOOD_TRANSLATION})
    check.check("correct is accepted", result.get("ok"), str(result.get("detail"))[:70])
    check.check(
        "and it says where the correction was written",
        "corrections.json" in str(result.get("detail")),
        str(result.get("detail")),
    )

    check.check(
        "the line on screen now shows the correction, with no OCR and no model",
        session._last_update.lines[0].target == GOOD_TRANSLATION,
        f"got {session._last_update.lines[0].target!r}",
    )
    check.check(
        "and it is marked fully covered, because a human answered the whole sentence",
        session._last_update.lines[0].coverage == 1.0
        and session._last_update.lines[0].confidence == 1.0,
        f"coverage={session._last_update.lines[0].coverage} "
        f"confidence={session._last_update.lines[0].confidence}",
    )
    check.check(
        "the aggregate text follows the line",
        session._last_update.target_text == GOOD_TRANSLATION,
    )
    check.check(
        "the history no longer shows the rejected translation",
        session.history[-1]["target"] == GOOD_TRANSLATION,
        f"got {session.history[-1]['target']!r}",
    )
    check.check("and the frame is marked as corrected", session.history[-1].get("corrected") is True)
    check.check(
        "the stale cache entry is gone",
        session._pipeline.recent.get(BAD_LINE, now=time.perf_counter()) is None,
        "otherwise the next frame shows the rejected translation again",
    )
    check.check(
        "while an unrelated line keeps its memory",
        session._pipeline.recent.get(UNRELATED, now=time.perf_counter()) == "The sword intent",
    )
    check.check(
        "the engine now translates the line the corrected way",
        session_corpus.translate(BAD_LINE).target_text == GOOD_TRANSLATION,
    )
    check.check(
        "the correction is in a file, so it outlives the process",
        corrections.corrections_path(session_config).exists(),
        str(corrections.corrections_path(session_config)),
    )

    published: list[dict] = []
    while True:
        try:
            published.append(channel.get_nowait())
        except queue.Empty:
            break
    kinds = [e.get("type") for e in published]
    check.check(
        "a correction event is published for surfaces that list corrections",
        EVENT_CORRECTION in kinds,
        f"{kinds}",
    )
    correction_event = next(e for e in published if e.get("type") == EVENT_CORRECTION)
    check.check(
        "carrying the source, the new text and the file",
        correction_event["data"].get("path") and correction_event["data"].get("total") == 1,
        str(correction_event["data"])[:80],
    )
    check.check(
        "and a repainted subtitle event, because a still screen produces no new frame",
        EVENT_SUBTITLE in kinds,
        f"{kinds}",
    )
    check.check(
        "in that order, so a surface sees the correction before the frame that uses it",
        kinds.index(EVENT_CORRECTION) < kinds.index(EVENT_SUBTITLE),
        f"{kinds}",
    )
    subtitle_event = [e for e in published if e.get("type") == EVENT_SUBTITLE][-1]
    check.check(
        "the repainted frame carries the corrected text",
        subtitle_event["data"]["lines"][0]["target"] == GOOD_TRANSLATION,
        str(subtitle_event["data"]["lines"][0])[:70],
    )

    # ---------------------------------------------------------------- #
    check.section("listing, undoing, and refusing bad input")

    listing = session.command(CMD_LIST_CORRECTIONS)
    check.check("list_corrections is accepted", listing.get("ok"))
    check.check(
        "and it reports the recorded correction",
        "1 correction" in str(listing.get("detail")),
        str(listing.get("detail"))[:70],
    )
    check.check(
        "the session exposes the same list to a UI",
        len(session.correction_listing()) == 1
        and session.correction_listing()[0]["source"] == BAD_LINE,
    )

    for label, payload in (
        ("a correction with no source", {"target": GOOD_TRANSLATION}),
        ("a correction with no target", {"source": BAD_LINE}),
        ("a correction whose target is only whitespace", {"source": BAD_LINE, "target": "   "}),
        ("a correction with an invented scope", {"source": BAD_LINE, "target": "x", "scope": "sentence"}),
    ):
        refused = session.command(CMD_CORRECT, payload)
        check.check(f"{label} is refused", not refused.get("ok"), str(refused.get("detail"))[:60])
    check.check(
        "and the refusal explains itself in the user's language",
        "source" in str(session.command(CMD_CORRECT, {"target": "x"}).get("detail")),
        str(session.command(CMD_CORRECT, {"target": "x"}).get("detail"))[:60],
    )
    check.check(
        "a refused correction writes nothing",
        len(session.correction_listing()) == 1,
        f"{len(session.correction_listing())} correction(s)",
    )

    removed = session.command(CMD_REMOVE_CORRECTION, {"source": BAD_LINE})
    check.check("remove_correction is accepted", removed.get("ok"), str(removed.get("detail"))[:60])
    check.check("and the correction is gone", len(session.correction_listing()) == 0)
    check.check(
        "so the engine goes back to what the corpus said",
        session_corpus.translate(BAD_LINE).target_text == BAD_TRANSLATION,
        "undo has to actually undo, or a typo in a correction is permanent",
    )
    check.check(
        "removing something that was never recorded is refused",
        not session.command(CMD_REMOVE_CORRECTION, {"source": "从未记录过"}).get("ok"),
    )
    check.check(
        "and removing with no source at all is refused",
        not session.command(CMD_REMOVE_CORRECTION, {}).get("ok"),
    )

    # ---------------------------------------------------------------- #
    check.section("the reload switches reach the live engine")

    live_corpus = session.corpus
    result = session.command(CMD_SET_CORPUS_RELOAD, {"auto_reload": False, "reload_interval_ms": 2000})
    check.check("set_corpus_reload is accepted", result.get("ok"), str(result.get("detail"))[:60])
    check.check(
        "the running corpus is switched off, not just the config",
        live_corpus.auto_reload is False,
        "a setting that only reaches config looks applied and does nothing",
    )
    check.check(
        "and the interval reaches it too",
        abs(live_corpus.reload_interval_s - 2.0) < 1e-9,
        f"reload_interval_s={live_corpus.reload_interval_s}",
    )
    check.check(
        "an empty set_corpus_reload is refused rather than silently ignored",
        not session.command(CMD_SET_CORPUS_RELOAD, {}).get("ok"),
    )
    session.command(CMD_SET_CORPUS_RELOAD, {"auto_reload": True, "reload_interval_ms": 500})

    applied = session.apply_settings({"corpus.reload_interval_ms": 1500})
    check.check("the settings panel path is accepted", applied.get("ok"), str(applied)[:70])
    check.check(
        "it is marked immediate and is actually applied immediately",
        "corpus.reload_interval_ms" in applied.get("applied_now", [])
        and abs(session.corpus.reload_interval_s - 1.5) < 1e-9,
        f"{applied.get('applied_now')} -> {session.corpus.reload_interval_s}",
    )
    check.check(
        "and it is written to the override file, so it is not forgotten at restart",
        "corpus" in AppConfig.load(tmp_project / "config.yaml").read_overrides(),
        str(AppConfig.load(tmp_project / "config.yaml").read_overrides().get("corpus")),
    )
    check.check(
        "the schema documents both reload fields",
        all(f"{c}.{n}" in _schema_keys() for c, n in (("corpus", "auto_reload"), ("corpus", "reload_interval_ms"))),
    )

    return check.report()


def _schema_keys() -> set[str]:
    from watashi import settings_schema

    return {field.key for field in settings_schema.all_fields()}


if __name__ == "__main__":
    sys.exit(main())
