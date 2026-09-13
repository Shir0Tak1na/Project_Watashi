#!/usr/bin/env python3
"""One word, several meanings. Headless: no screen, no models.

The user asked three questions, and this file is the answer to the first two, asserted:

* **"if a word has different meanings, do we need separate configuration?"** It used to be
  that the question could not even be expressed. Entries were keyed on
  ``(language, source)``, so the second meaning of a term landed on the same key as the
  first and one of them was discarded -- silently, with no counter, no warning, and no
  way to find out except by noticing a translation that never changed. The key is now
  ``(language, source, scene)``, and a collision is recorded and reported.
* **"with several meanings in one configuration, how is the context decided?"** Four
  things in order, and they are all asserted here: an entry may name the **scene** it
  belongs to (``domain``, selected with the 当前场景 setting), the **line** it belongs to
  (``when_line``), the **company** it keeps (``when_near``), and the **window** it came
  from (``when_window``). Layer still outranks scene: the user's own file is their word on
  the matter and a scene tag on a shipped entry must not override it.

The third question -- bulk entry -- is covered by ``selfcheck_library`` plus the import
and promotion sections of this file.

Two of the assertions here are for defects that were found by reading rather than by use,
and both were user-visible wrong output:

* the recent-translation memory was keyed on the source text **alone**, so switching the
  target language inside its 10 s TTL served the previous language's text back;
* the corrections file was keyed on the source text alone too, so a correction written
  while translating into Chinese kept winning after a switch to Japanese.

    run.cmd selfcheck_senses --summary
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker  # noqa: E402

from watashi import correct as corrections_module  # noqa: E402
from watashi.config import AppConfig  # noqa: E402
from watashi.recent import RecentTranslations  # noqa: E402
from watashi.session import build_corpus  # noqa: E402
from watashi.translate import Context, CorpusStore  # noqa: E402

ZH = "zh-CN"
JA = "ja"


def write_corpus(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def store(user: Path, domain: Path | None = None) -> CorpusStore:
    """A corpus with only the layers this check writes into."""
    config = AppConfig.load()
    config.corpus["user"] = [str(user)]
    config.corpus["domain"] = [str(domain)] if domain else []
    config.corpus["general"] = []
    return build_corpus(config)


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("One word, several meanings (no screen, no models)")
    print("=" * 78)

    work = Path(tempfile.mkdtemp(prefix="watashi-senses-"))
    user = work / "user"
    domain = work / "domain"

    # ------------------------------------------------------------------ #
    check.section("two meanings of one word can both be stored")
    #
    # The file is written the way a user would write it after reading the panel's help:
    # one term, a list of answers, each naming the scene it belongs to. Before scenes
    # existed this loaded as one entry and lost the other without saying so.
    write_corpus(
        user / "senses.json",
        {
            "lang": ZH,
            "entries": {
                "bank": [
                    {"target": "银行", "domain": "finance"},
                    {"target": "岸", "domain": "geography"},
                ],
                "crane": [
                    {"target": "起重机", "domain": "construction"},
                    {"target": "鹤", "domain": "wildlife"},
                ],
                "seal": "海豹",
            },
        },
    )
    live = store(user)

    check.check(
        "both meanings load, instead of one silently replacing the other",
        live.size == 5,
        f"{live.size} entries: {sorted(e.target for e in live.entries_snapshot())}",
    )
    check.check(
        "and the scenes they declare are listed, so a picker has something to offer",
        live.domains == ["construction", "finance", "geography", "wildlife"],
        f"{live.domains}",
    )
    check.check(
        "with no scene selected there is no conflict at all, because nothing collided",
        live.conflicts == [],
        f"{live.conflicts}",
    )

    # ------------------------------------------------------------------ #
    check.section("the scene decides which meaning is used")
    for scene, expected_bank, expected_crane in (
        ("finance", "银行", "起重机"),
        ("geography", "岸", "起重机"),
        ("construction", "银行", "起重机"),
        ("wildlife", "银行", "鹤"),
    ):
        context = Context(target_lang=ZH, scene=scene)
        bank = live.lookup_exact("bank", ZH, context)
        crane = live.lookup_exact("crane", ZH, context)
        check.check(
            f"scene={scene}: bank -> {expected_bank}, crane -> {expected_crane}",
            bank is not None
            and bank.target == expected_bank
            and crane is not None
            and crane.target == expected_crane,
            f"bank={bank.target if bank else None} crane={crane.target if crane else None}",
        )

    untouched = live.lookup_exact("seal", ZH, Context(target_lang=ZH, scene="finance"))
    check.check(
        "a term with one answer is unaffected by the scene",
        untouched is not None and untouched.target == "海豹",
        f"{untouched.target if untouched else None}",
    )

    # The unselected case has to stay exactly as it was, or every existing corpus changes
    # behaviour the moment this feature ships.
    neutral = store(user)
    check.check(
        "with no scene selected the answer is the same one, deterministically",
        neutral.lookup_exact("bank", ZH).target == "银行"
        and store(user).lookup_exact("bank", ZH).target == "银行",
        f"{neutral.lookup_exact('bank', ZH).target}",
    )

    # A corpus that names no scene and tests no window must key its caches on the target
    # language alone, or every existing install loses cache hits to dimensions that
    # cannot change an answer.
    sceneryless = work / "sceneryless"
    write_corpus(sceneryless / "plain.json", {"lang": ZH, "entries": {"bank": "银行"}})
    bare = store(sceneryless)
    check.check(
        "a corpus with no scenes keys its caches on the target language alone",
        bare.context_key(Context(target_lang=ZH)) == (ZH,),
        f"{bare.context_key(Context(target_lang=ZH))}",
    )
    check.check(
        "so a window it cannot see does not split its cache",
        bare.context_key(Context(target_lang=ZH, window="Anything")) == (ZH,),
        f"{bare.context_key(Context(target_lang=ZH, window='Anything'))}",
    )
    check.check(
        "a corpus with scenes adds the scene dimension, and only that",
        neutral.context_key(Context(target_lang=ZH, scene="finance"))
        == (ZH, "finance")
        and neutral.context_key(Context(target_lang=ZH, window="Anything"))
        == (ZH, ""),
        f"{neutral.context_key(Context(target_lang=ZH, scene='finance'))} / "
        f"{neutral.context_key(Context(target_lang=ZH, window='Anything'))}",
    )
    check.check(
        "scene names are compared case- and space-insensitively",
        live.lookup_exact("bank", ZH, Context(target_lang=ZH, scene="  Finance ")).target
        == "银行",
        "a hand-edited file must not need exact spelling to match",
    )

    # ------------------------------------------------------------------ #
    check.section("the user's own file still wins over a scene tag")
    #
    # This is the ordering that matters most: a user who writes an override means it.
    # A shipped entry tagged with the scene the user happens to be in must not defeat it.
    # Isolated from the corpora above on purpose -- those put a scene-tagged entry in the
    # user layer too, and within one layer the scene is *supposed* to decide.
    layered_user = work / "layered-user"
    write_corpus(
        domain / "shipped.json",
        {"lang": ZH, "entries": {"crane": {"target": "鹤", "domain": "wildlife"}}},
    )
    write_corpus(
        layered_user / "mine.json",
        {"lang": ZH, "entries": {"crane": {"target": "吊车"}}},
    )
    layered = store(layered_user, domain)
    hit = layered.lookup_exact("crane", ZH, Context(target_lang=ZH, scene="wildlife"))
    check.check(
        "the user's untagged entry beats the shipped entry tagged with the current scene",
        hit is not None and hit.target == "吊车" and hit.layer == "user",
        f"{hit.target if hit else None} from {hit.layer if hit else None}",
    )
    check.check(
        "and the shipped entry is still there, listed, so the editor can show both",
        {e.target for e in layered.senses("crane", ZH)} == {"吊车", "鹤"},
        f"{[e.target for e in layered.senses('crane', ZH)]}",
    )
    check.check(
        "within one layer, though, the scene does decide",
        {e.target for e in store(user).senses("crane", ZH)} == {"起重机", "鹤"}
        and store(user)
        .lookup_exact("crane", ZH, Context(target_lang=ZH, scene="wildlife"))
        .target
        == "鹤",
        "otherwise a scene tag would be decorative",
    )

    # ------------------------------------------------------------------ #
    check.section("conditions: the line, the company, the window")
    #
    # Its own directory: a condition test mixed with the scene corpus above would be
    # passing or failing for reasons that belong to the other one. The three pairs below
    # are each two meanings of one term, told apart *only* by their conditions -- which is
    # the case that used to be impossible to write down, because both landed on one key.
    conditional_dir = work / "conditional"
    write_corpus(
        conditional_dir / "conditional.json",
        {
            "lang": ZH,
            "entries": {
                "bank": [
                    {"target": "银行", "when_near": ["account", "deposit", "loan"]},
                    {"target": "岸", "when_line": r"\briver\b"},
                    {"target": "堤", "when_near": ["bank"]},
                ],
                "void": [
                    {"target": "虚空", "when_window": "Novel"},
                    {"target": "空白", "when_window": "Editor"},
                ],
            },
        },
    )
    conditional = store(conditional_dir)
    check.check(
        "two meanings told apart only by their conditions both survive loading",
        len(conditional.senses("bank", ZH)) == 3 and len(conditional.senses("void", ZH)) == 2,
        f"bank={len(conditional.senses('bank', ZH))} void={len(conditional.senses('void', ZH))}",
    )

    def translate(line: str, scene: str = "", window: str = "") -> str:
        return conditional.translate(
            line, ZH, Context(target_lang=ZH, scene=scene, window=window)
        ).target_text

    def answering(line: str, window: str = "") -> list[tuple[str, str]]:
        """``(source span, target)`` for the parts a **corpus entry** answered.

        Asserted on provenance rather than on the composed sentence, and restricted to
        ``corpus:`` origins on purpose: what is under test is which *entry* answered. The
        composed text also carries the unmatched words around it, and the shipped word-formation
        rules answer every token they see with the token itself, so counting anything
        ``explained`` would include a transliterate span for each ordinary word.
        """
        outcome = conditional.translate(
            line, ZH, Context(target_lang=ZH, window=window)
        )
        return [
            (span.source, span.target)
            for span in outcome.spans
            if span.origin.startswith("corpus:")
        ]

    check.check(
        "when_near picks the financial meaning when its company is present",
        answering("open a bank account") == [("bank", "银行")],
        f"{answering('open a bank account')}",
    )
    check.check(
        "and when_near does not count the term being conditioned on as its own company",
        # The term is cut out of the line before neighbours are looked for. Here the only
        # word is the term itself, so the 堤 entry -- whose condition names the term --
        # must not answer: a condition that cannot fail is a condition the author did not
        # mean to write. Nothing else applies either, so the line passes through.
        answering("the bank") == [] and translate("the bank") == "the bank",
        f"{answering('the bank')} -> {translate('the bank')!r}",
    )
    check.check(
        "when_near does count a genuine second occurrence",
        # Both occurrences are answered, and that is the honest reading of a condition
        # that says "when this term appears again in the line": for each of the two spans
        # the other one is the company. The assertion is that the condition is not dead,
        # which the self-only line above shows it would be if the term were not cut out.
        answering("bank to bank") == [("bank", "堤"), ("bank", "堤")],
        f"{answering('bank to bank')}",
    )
    check.check(
        "an entry whose conditions do not hold is not used at all",
        answering("the void") == [] and translate("the void") == "the void",
        translate("the void"),
    )
    check.check(
        "when_line selects on the whole line, not on the term",
        # Neither line contains the term, so neither may be answered by a term entry --
        # a condition that could match anything would be a term that translates everywhere.
        answering("down by the river") == [] and answering("river") == [],
        f"{answering('down by the river')} / {answering('river')}",
    )
    check.check(
        "a term in a line that satisfies its condition is answered by that meaning",
        answering("river bank") == [("bank", "岸")],
        f"{answering('river bank')}",
    )
    check.check(
        "and the same term in the other order picks the same meaning",
        answering("bank river") == [("bank", "岸")],
        f"{answering('bank river')}",
    )
    check.check(
        "when_window selects by the window the text came from",
        translate("void", window="Chapter 3 - Novel") == "虚空"
        and translate("void", window="Editor - notes.txt") == "空白",
        f"{translate('void', window='Chapter 3 - Novel')} / "
        f"{translate('void', window='Editor - notes.txt')}",
    )
    check.check(
        "an entry that requires a window is not applied to text with no window",
        translate("void") == "void",
        "a fixed region has no title, and a scene term must not leak into it: "
        + translate("void"),
    )
    check.check(
        "the two meanings answer different windows, so neither is dead code",
        {translate("void", window="a Novel") , translate("void", window="an Editor")}
        == {"虚空", "空白"},
        "both entries must be reachable, or the list is decoration",
    )
    check.check(
        "a conditional entry that does not apply does not block the ones that do",
        # The financial condition fails here and the river one holds, so the span must be
        # answered rather than swallowed by whichever candidate was considered first.
        answering("bank river") != [],
        f"{answering('bank river')}",
    )
    check.check(
        "and the conditions the vocabulary uses are part of the cache key",
        conditional.context_key(Context(target_lang=ZH, window="A")) != (ZH,),
        "window conditions must split the caches, or one window's answer is served "
        "in another",
    )

    # ------------------------------------------------------------------ #
    check.section("a real collision is reported rather than swallowed")
    rival_user = work / "rival-user"
    write_corpus(
        rival_user / "rival.json",
        {"lang": ZH, "entries": {"seal": {"target": "印章", "domain": "legal"}}},
    )
    write_corpus(
        domain / "rival.json",
        {"lang": ZH, "entries": {"seal": {"target": "封条", "domain": "legal"}}},
    )
    rival = store(rival_user, domain)
    check.check(
        "the same source, language, scene and conditions twice is one collision",
        len(rival.conflicts) == 1,
        f"{rival.conflicts}",
    )
    conflict = rival.conflicts[0]
    check.check(
        "the collision says which one was kept and which was dropped",
        conflict["kept"]["target"] == "印章" and conflict["dropped"]["target"] == "封条",
        json.dumps(conflict, ensure_ascii=False),
    )
    check.check(
        "and it names the layer that decided it",
        conflict["kept"]["layer"] == "user" and conflict["dropped"]["layer"] == "domain",
        f"{conflict['kept']['layer']} over {conflict['dropped']['layer']}",
    )
    check.check(
        "the dropped entry is not silently gone: the reason is a sentence a person reads",
        "higher layer" in conflict["why"],
        conflict["why"],
    )
    print(f"      reported as: {conflict['why']}")

    # Two entries that agree on the answer are the same statement written twice. Reported
    # as a conflict they would be noise in a list the user is meant to act on, and noise
    # is how a diagnostic stops being read.
    same_user = work / "same-user"
    write_corpus(
        same_user / "same.json", {"lang": ZH, "entries": {"seal": {"target": "海豹"}}}
    )
    write_corpus(
        domain / "same.json", {"lang": ZH, "entries": {"seal": {"target": "海豹"}}}
    )
    check.check(
        "the same answer in two layers is not reported as a conflict",
        store(same_user, domain).conflicts == [],
        f"{store(same_user, domain).conflicts}",
    )

    # ------------------------------------------------------------------ #
    check.section("the memories cannot serve one situation's answer in another")
    #
    # Both of these were real defects, found by reading the keys rather than by using the
    # program: the source text was the entire key, so "the same line" meant "the same
    # line, whatever else is true".
    memory = RecentTranslations()
    memory.put("He broke through to the void realm", "他突破到了虚空境界", scope=(ZH,))
    check.check(
        "the same line in the same situation is reused",
        memory.get("He broke through to the void realm", scope=(ZH,))
        == "他突破到了虚空境界",
    )
    check.check(
        "the same line for a different target language is not",
        memory.get("He broke through to the void realm", scope=(JA,)) is None,
        "this was the bug: a Chinese line handed back as the answer for Japanese",
    )
    check.check(
        "and not across scenes either",
        memory.get("He broke through to the void realm", scope=(ZH, "finance")) is None,
        "an answer chosen for one scene must not answer another",
    )
    memory.put("a longer line for the scoped test", "SCOPED", scope=(ZH, "finance"))
    check.check(
        "a scoped entry is found under its own scope",
        memory.get("a longer line for the scoped test", scope=(ZH, "finance")) == "SCOPED",
        "otherwise the scope is not a key but a bug",
    )
    dropped = memory.drop("He broke through to the void realm")
    check.check(
        "dropping a source clears it in every scope at once",
        dropped == 1
        and memory.get("He broke through to the void realm", scope=(ZH,)) is None
        and memory.get("He broke through to the void realm", scope=(JA,)) is None,
        f"dropped={dropped}",
    )

    # ------------------------------------------------------------------ #
    check.section("corrections know what language they were written in")
    corrections_path = user / "corrections.json"
    box = corrections_module.Corrections(corrections_path)
    line = "He broke through to the void realm"
    box.apply(line, "他突破到了虚空境界", lang=ZH)
    box.apply(line, "彼は虚空境界に突破した", lang=JA)
    check.check(
        "the same line can be corrected differently for two languages",
        len(box) == 2,
        f"{[c.to_dict() for c in box.items()]}",
    )
    check.check(
        "each language gets its own answer, in both directions",
        box.lookup_line(line, ZH).target == "他突破到了虚空境界"
        and box.lookup_line(line, JA).target == "彼は虚空境界に突破した",
        f"{box.lookup_line(line, ZH).target} / {box.lookup_line(line, JA).target}",
    )
    check.check(
        "a third language gets nothing rather than the wrong language",
        box.lookup_line(line, "ko") is None,
        "the bug was that it got the Chinese one",
    )
    raw = json.loads(corrections_path.read_text(encoding="utf-8"))
    check.check(
        "the file holds both, as a list, because JSON cannot hold one key twice",
        isinstance(raw["entries"][line], list) and len(raw["entries"][line]) == 2,
        json.dumps(raw["entries"][line], ensure_ascii=False)[:160],
    )
    check.check(
        "the language is written, so the next read agrees with this one",
        {row.get("lang") for row in raw["entries"][line]} == {ZH, JA},
        f"{raw['entries'][line]}",
    )

    # An untagged correction is what every pre-existing corrections file contains, and it
    # keeps its old permissive meaning -- but the panel is told about it.
    legacy_path = work / "legacy" / "corrections.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps({"entries": {line: {"target": "旧译法", "scope": "line"}}},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    legacy = corrections_module.Corrections(legacy_path)
    check.check(
        "a correction with no language still applies, so old files keep working",
        legacy.lookup_line(line, JA) is not None
        and legacy.lookup_line(line, JA).target == "旧译法",
        "refusing them would silently discard the user's own work on upgrade",
    )
    check.check(
        "and it is listed as untagged, so the panel can offer to label it",
        [c.target for c in legacy.untagged_line_corrections()] == ["旧译法"],
        f"{[c.target for c in legacy.untagged_line_corrections()]}",
    )
    check.check(
        "removing one language leaves the other",
        box.remove(line, lang=ZH) and len(box) == 1 and box.lookup_line(line, JA) is not None,
        f"{[c.to_dict() for c in box.items()]}",
    )

    # ------------------------------------------------------------------ #
    check.section("senses do not cost the hot path")
    #
    # A dict get and a length check per candidate substring is what this scan costs, and
    # that must not change for a corpus that has no senses in it. Measured rather than
    # assumed: medians of 300 runs, compared between a plain corpus and one carrying
    # conditions, with the bound loose enough to survive a busy machine.
    plain = store(user / "plain")
    write_corpus(
        user / "plain" / "many.json",
        {"lang": ZH, "entries": {f"term{i}": f"译{i}" for i in range(200)}},
    )
    plain.load()
    heavy = store(user / "heavy")
    write_corpus(
        user / "heavy" / "many.json",
        {
            "lang": ZH,
            "entries": {
                **{f"term{i}": f"译{i}" for i in range(200)},
                "bank": [
                    {"target": "银行", "when_near": ["account"]},
                    {"target": "岸", "when_line": "river"},
                ],
            },
        },
    )
    heavy.load()
    sample = "a line with no corpus term in it at all, only ordinary words to scan past"

    def median_ms(store_: CorpusStore, repeats: int = 300) -> float:
        timings = []
        for _ in range(repeats):
            started = time.perf_counter()
            store_.translate(sample, ZH)
            timings.append((time.perf_counter() - started) * 1000.0)
        timings.sort()
        return timings[len(timings) // 2]

    plain_ms = median_ms(plain)
    heavy_ms = median_ms(heavy)
    print(f"      median scan: {plain_ms:.3f} ms plain, {heavy_ms:.3f} ms with senses")
    check.check(
        "a corpus with senses scans at the same cost as one without",
        heavy_ms <= max(plain_ms * 3.0, plain_ms + 0.05),
        f"{plain_ms:.3f} ms vs {heavy_ms:.3f} ms",
    )
    check.check(
        "and a line that does contain a sense is resolved, not skipped",
        heavy.translate("open a bank account", ZH).target_text == "open a 银行 account",
        heavy.translate("open a bank account", ZH).target_text,
    )

    # ------------------------------------------------------------------ #
    check.section("the pipeline actually passes the scene, end to end")
    #
    # The assertions above test the pieces. This one tests the wiring between them, and it
    # is the one that would catch the whole feature silently not being connected: the same
    # line, two frames, two scenes, and the answer has to change. A recent-translation
    # memory that is handed the target language but not the scene would serve the first
    # scene's answer to the second, and every unit assertion above would still pass.
    from watashi.events import CMD_SET_SCENE
    from watashi.session import Session
    from watashi.synth import SyntheticCapturer
    from watashi.translate import CorpusTranslator

    wire = work / "wired"
    write_corpus(
        wire / "user" / "senses.json",
        {
            "lang": ZH,
            "entries": {
                "the void realm": [
                    {"target": "虚空界", "domain": "cultivation"},
                    {"target": "虚空领域", "domain": "science"},
                ]
            },
        },
    )
    wire_config = AppConfig.load()
    wire_config.base_dir = wire
    # Hermetic: this session reads only the file written above, so a shipped corpus that
    # happens to contain one of these words cannot be what makes the assertion pass.
    wire_config.corpus["user"] = [str(wire / "user")]
    wire_config.corpus["domain"] = []
    wire_config.corpus["general"] = []
    wire_config.translation["scene"] = "cultivation"
    wire_config.translation["nmt_model"] = None
    # Both frames read the same way: the point is that the *scene* changed between them,
    # not the text. Change detection off, for the reason selfcheck_dedup documents --
    # otherwise the second frame never reaches OCR and this measures the detector.
    wire_config.capture["diff_threshold"] = 0.0
    wire_corpus = build_corpus(wire_config)
    wire_session = Session(
        wire_config,
        capturer=SyntheticCapturer(
            frames=[("the void realm holds",), ("the void realm holds.",)],
            hold_seconds=1.2,
        ),
        translator=CorpusTranslator(wire_corpus),
    )
    wire_session.build()
    wire_channel = wire_session.subscribe()
    wire_session.start()

    frames: list[str] = []
    switched = False
    deadline = time.perf_counter() + 6.0
    while time.perf_counter() < deadline:
        try:
            event = wire_channel.get(timeout=0.1)
        except Exception:
            event = None
        if event is not None and event.get("type") == "subtitle":
            # "target", not "target_text": the envelope is the wire form, and
            # decode_update is what turns it back into a target_text.
            text = str(event.get("data", {}).get("target") or "")
            frames.append(text)
            if not switched:
                # Mid-run, exactly as a user would: the scene is a live setting.
                switched = True
                moved = wire_session.command(CMD_SET_SCENE, {"scene": "science"})
                check.check(
                    "set_scene applies mid-run",
                    moved.get("ok") and wire_session.pipeline.config.scene == "science",
                    json.dumps(moved, ensure_ascii=False)[:120],
                )
        if len(frames) >= 2:
            break
    wire_session.stop()

    check.check(
        "two frames were translated, so this is a comparison and not a single sample",
        len(frames) >= 2,
        f"{frames}",
    )
    check.check(
        "the same line answers with the first scene before the switch",
        frames and "虚空界" in frames[0],
        f"first={frames[0] if frames else None}",
    )
    check.check(
        "and with the second scene after it, so the scene reaches the caches, not just the corpus",
        len(frames) >= 2 and "虚空领域" in frames[1],
        f"second={frames[1] if len(frames) >= 2 else None} "
        f"(reused={wire_session.pipeline.reused_lines})",
    )

    # ------------------------------------------------------------------ #
    check.section("the session exposes it, and it survives a reload")
    config = AppConfig.load()
    config.base_dir = work / "session"
    config.base_dir.mkdir(parents=True, exist_ok=True)
    config.corpus["user"] = [str(user)]
    config.corpus["domain"] = [str(domain)]
    config.corpus["general"] = []
    config.translation["scene"] = ""
    session = Session(config, capturer=SyntheticCapturer())
    session.build()
    check.check(
        "the session starts with no scene, i.e. exactly the old behaviour",
        session.pipeline.config.scene == "",
        session.pipeline.config.scene,
    )
    result = session.command(CMD_SET_SCENE, {"scene": "finance"})
    check.check(
        "set_scene is accepted and applied to the running pipeline",
        result.get("ok") and session.pipeline.config.scene == "finance",
        json.dumps(result, ensure_ascii=False)[:160],
    )
    check.check(
        "the command reports which scene names the corpus actually has",
        "finance" in result.get("detail", ""),
        result.get("detail", ""),
    )
    check.check(
        "changing the scene moves the vocabulary revision, so in-flight work is dropped",
        session.corpus.revision > 0,
        f"revision={session.corpus.revision}",
    )
    check.check(
        "and the state a surface reads carries it",
        session.command("status", {}).get("scene") == "finance",
        json.dumps(session.command("status", {}), ensure_ascii=False)[:200],
    )
    unknown = session.command(CMD_SET_SCENE, {"scene": "not-a-scene"})
    check.check(
        "an unknown scene is accepted but says so, rather than looking like it worked",
        unknown.get("ok") and "没有词条标这个场景" in session._status,
        session._status[:140],
    )
    session.command(CMD_SET_SCENE, {"scene": ""})
    check.check(
        "clearing the scene restores the neutral answer",
        session.pipeline.config.scene == ""
        and session.corpus.lookup_exact("bank", ZH).target == "银行",
        session.pipeline.config.scene,
    )
    session.stop()

    # ------------------------------------------------------------------ #
    check.section("promotion through the session, which is what the button calls")
    #
    # The conversion is asserted below in isolation; this is the path a user actually
    # triggers, so it is checked end to end: record a correction the way the correction
    # editor does, promote it, and require the corpus to answer with it afterwards while
    # the corrections file itself is left alone.
    promote_config = AppConfig.load()
    promote_config.base_dir = work / "promote-session"
    promote_config.base_dir.mkdir(parents=True, exist_ok=True)
    promote_config.corpus["user"] = [str(promote_config.base_dir / "user")]
    promote_config.corpus["domain"] = []
    promote_config.corpus["general"] = []
    promote_config.translation["target"] = ZH
    promote_session = Session(promote_config, capturer=SyntheticCapturer())
    promote_session.build()
    promoted_line = "He broke through to the void realm today"
    recorded = promote_session.command(
        "correct",
        {"source": promoted_line, "target": "他今日突破到了虚空境界", "scope": "line"},
    )
    corrections_file = promote_session.corpus.corrections.path
    stored = promote_session.corpus.corrections.lookup_exact(promoted_line, ZH)
    check.check(
        "a correction records the target language it was written in",
        recorded.get("ok") and stored is not None and stored.lang == ZH,
        f"ok={recorded.get('ok')} stored={stored.to_dict() if stored else None}",
    )
    check.check(
        "and the language reaches the file, not just the in-memory row",
        corrections_file.is_file()
        and json.loads(corrections_file.read_text(encoding="utf-8"))["entries"][promoted_line][
            "lang"
        ]
        == ZH,
        corrections_file.read_text(encoding="utf-8")[:200] if corrections_file.is_file() else "no file",
    )
    before_bytes = corrections_file.read_bytes() if corrections_file.is_file() else b""

    preview = promote_session.command("library_promote", {"dry_run": True})
    check.check(
        "promotion previews without writing, like the import preview does",
        preview.get("ok") and "would become entries" in preview.get("detail", ""),
        json.dumps(preview, ensure_ascii=False)[:180],
    )
    check.check(
        "and the preview really did not write the entry",
        promote_session.library().find(promoted_line, ZH) is None,
        f"{promote_session.library().entries}",
    )
    result = promote_session.command("library_promote", {})
    check.check(
        "promoting turns the correction into a corpus entry",
        result.get("ok")
        and promote_session.library().find(promoted_line, ZH) is not None,
        json.dumps(result, ensure_ascii=False)[:180],
    )
    check.check(
        "and the engine answers with it, so the round trip is real",
        promote_session.corpus.lookup_exact(promoted_line, ZH) is not None,
        f"lookup: {promote_session.corpus.lookup_exact(promoted_line, ZH)}",
    )
    check.check(
        "promotion reads one file and writes another: the corrections file is untouched",
        corrections_file.is_file() and corrections_file.read_bytes() == before_bytes,
        f"{corrections_file}",
    )
    second = promote_session.command("library_promote", {})
    check.check(
        "promoting twice is idempotent rather than compounding",
        second.get("ok") and "added 0" in second.get("detail", ""),
        second.get("detail", ""),
    )

    # The count of untagged corrections has to be reachable from a surface, or "the engine
    # reports them" is a claim about a method nobody calls. `Corrections.stats()` existed
    # and was called by nobody until this was published through the translator's stats.
    box3 = corrections_module.Corrections(corrections_file)
    box3.apply("A line nobody tagged a language for", "没有人给它标语言", scope="line")
    promote_session.corpus.load()
    untagged_line = "A line nobody tagged a language for"
    info = promote_session.info()
    check.check(
        "an untagged correction is counted where a surface can read it",
        info.get("corrections_untagged", 0) == 1 and info.get("corrections", 0) >= 1,
        f"corrections={info.get('corrections')} untagged={info.get('corrections_untagged')}",
    )
    check.check(
        "and it still applies, so the count describes a working correction and not a broken one",
        promote_session.corpus.corrections.lookup_line(untagged_line, JA) is not None,
        "untagged means 'any target', which is what every pre-existing file is",
    )
    promote_session.stop()

    # ------------------------------------------------------------------ #
    check.section("bulk entry: the promotion path a corpus editor needs")
    #
    # Corrections and the corpus are separate files on purpose -- one is what a human
    # said about a line, the other is vocabulary -- but a user who has just corrected
    # thirty lines while watching should not have to retype them as entries.
    box2 = corrections_module.Corrections(work / "promote" / "corrections.json")
    line_a = "He broke through to the void realm yesterday"
    line_b = "The sword spirit awakened within him"
    box2.apply(line_a, "他昨日突破到了虚空境界", lang=ZH)
    box2.apply(line_b, "剑灵在他体内苏醒", scope=corrections_module.SCOPE_TERM, lang=ZH)
    check.check(
        "both scopes are listed, so a surface can offer them for promotion",
        len(box2.describe()) == 2,
        f"{[c['scope'] for c in box2.describe()]}",
    )
    from watashi import library as library_module  # noqa: E402

    promoted = library_module.library_from_corrections(box2, lang=ZH)
    entries, problems = library_module.entries_from_payload(promoted)
    check.check(
        "promotion produces entries the corpus loader already understands",
        len(entries) == 2 and not problems,
        f"{len(entries)} entries, problems={problems}",
    )
    check.check(
        "and it keeps the language, so promoting does not create an untagged entry",
        all(entry.lang == ZH for entry in entries),
        f"{[entry.lang for entry in entries]}",
    )
    check.check(
        "and it does not modify the corrections file it read",
        len(json.loads((work / "promote" / "corrections.json").read_text(encoding="utf-8"))["entries"]) == 2,
        "promotion is a read of one file and a write of another",
    )

    return check.report()


if __name__ == "__main__":
    sys.exit(main())
