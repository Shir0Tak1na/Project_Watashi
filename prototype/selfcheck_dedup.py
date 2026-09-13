#!/usr/bin/env python3
"""Text reuse verification. Headless: no screen, no models.

The value here is not "the same line is not shown twice"; it is that the *model* is not
paid for text it has already translated. OCR is not deterministic, so the same sentence
returns as ``他突破到了虚空境界`` and then ``他突破到了虚空境界。`` -- different cache keys,
both missing the translation cache. So the assertions below are about calls avoided,
and the normalisation that decides whether any of it matches is tested directly,
including the cases where it must *not* match.

    run.cmd selfcheck_dedup --summary
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker  # noqa: E402

from watashi.recent import MIN_KEY_LENGTH, RecentTranslations, normalize


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("Text reuse self check (no screen, no models)")
    print("=" * 78)

    # ---------------------------------------------------------------- #
    check.section("normalisation matches what OCR actually varies")
    variants = [
        "他突破到了虚空境界",
        "他突破到了虚空境界。",
        "他突破到了虚空境界 ",
        " 他突破到了虚空境界",
        "他突破到了虚空境界！",
        "他突破到了虚空境界…",
    ]
    keys = {normalize(v) for v in variants}
    check.check(
        "trailing punctuation and spacing do not change the key",
        len(keys) == 1,
        f"{len(keys)} distinct key(s) from {len(variants)} variants",
    )
    check.check(
        "internal punctuation DOES change it",
        normalize("好，我来") != normalize("好我来"),
        "collapsing these would merge genuinely different sentences",
    )
    check.check(
        "Latin case does not change it",
        normalize("The Void Realm") == normalize("the void realm"),
    )
    check.check(
        "different sentences stay different",
        normalize("他突破到了虚空境界") != normalize("他退出了虚空境界"),
    )

    # ---------------------------------------------------------------- #
    check.section("reuse inside the window, re-translate outside it")
    memory = RecentTranslations(ttl_s=10.0)
    check.check("nothing is known at first", memory.get("他突破到了虚空境界", now=0.0) is None)
    memory.put("他突破到了虚空境界", "He broke through to the void realm", now=0.0)

    check.check(
        "a punctuation variant is reused",
        memory.get("他突破到了虚空境界。", now=1.0)
        == "He broke through to the void realm",
    )
    check.check("and it counted as a hit", memory.hits == 1, f"hits={memory.hits}")
    check.check(
        "a lookup for something never seen is a miss",
        memory.get("完全不同的一句话在这里", now=1.0) is None,
    )
    # The window slides: every reuse refreshes it. That is deliberate and is the right
    # behaviour for this problem -- if the sentence is still on screen, its translation
    # is still the right answer, however long it has been there.
    check.check(
        "while it keeps being seen, it stays valid",
        memory.get("他突破到了虚空境界", now=5.0)
        == "He broke through to the void realm",
        "the window slides, so a line still on screen is still valid",
    )
    check.check(
        "once it stops being seen for the whole window, it is translated again",
        memory.get("他突破到了虚空境界", now=16.0) is None,
        "last seen at 5.0, window is 10.0s",
    )
    check.check(
        "and the expired entry is dropped, not merely ignored",
        len(memory) == 0,
        f"size={len(memory)}",
    )

    # ---------------------------------------------------------------- #
    check.section("short lines are never remembered")
    short = RecentTranslations(ttl_s=10.0)
    short.put("是", "yes", now=0.0)
    short.put("OK", "好", now=0.0)
    check.check(
        "a one or two character line is not stored",
        len(short) == 0,
        f"size={len(short)} (floor is {MIN_KEY_LENGTH} characters)",
    )
    check.check(
        "so it cannot be reused by collision",
        short.get("是", now=0.5) is None,
    )

    # ---------------------------------------------------------------- #
    check.section("the memory is bounded and keeps what recurs")
    bounded = RecentTranslations(ttl_s=100.0, limit=3)
    for index in range(5):
        bounded.put(f"第{index}句完全不同的内容", f"line {index}", now=float(index))
    check.check("it does not grow past its limit", len(bounded) == 3, f"size={len(bounded)}")

    keep = RecentTranslations(ttl_s=100.0, limit=3)
    keep.put("这一句会反复出现", "recurring", now=0.0)
    # The recurring line is looked up on *every* round, which is what a line that stays
    # on screen actually does. Reusing it once and then inserting three newer lines
    # leaves it genuinely the least recently used, so an earlier version of this test
    # was asserting the opposite of correct LRU behaviour.
    for index in range(6):
        keep.put(f"第{index}句一次性内容", f"once {index}", now=float(index + 1))
        keep.get("这一句会反复出现", now=float(index + 1))
    check.check("it stayed within its limit", len(keep) == 3, f"size={len(keep)}")
    check.check(
        "a line that keeps appearing survives a stream of one-off lines",
        keep.get("这一句会反复出现", now=9.0) == "recurring",
        f"still known: {sorted(keep._entries)}",  # noqa: SLF001 - introspection under test
    )
    check.check(
        "while the one-off lines from the start are gone",
        keep.get("第0句一次性内容", now=9.0) is None,
    )

    # ---------------------------------------------------------------- #
    check.section("switching it off really switches it off")
    off = RecentTranslations(ttl_s=10.0, enabled=False)
    off.put("他突破到了虚空境界", "whatever", now=0.0)
    check.check("nothing is stored when disabled", len(off) == 0)
    check.check("and nothing is returned", off.get("他突破到了虚空境界", now=0.0) is None)

    # ---------------------------------------------------------------- #
    check.section("END TO END: the same on-screen text is not translated twice")
    # A unit test of the memory proves the memory works; it does not prove the pipeline
    # uses it. This drives a real Session over frames that repeat the same sentence and
    # counts how many times the translator was actually asked to work.
    from watashi.config import AppConfig
    from watashi.session import Session
    from watashi.synth import SyntheticCapturer

    class CountingTranslator:
        """Stands in for the corpus+model chain and counts its calls."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        def translate(self, text: str, target_lang: str = "zh-CN"):  # noqa: ANN201
            from watashi.translate import Outcome, Span

            self.calls.append(text)
            return Outcome(
                source_text=text,
                target_text=f"[{len(self.calls)}] {text}",
                spans=[Span(source=text, target=f"[{len(self.calls)}]", origin="stub",
                            confidence=1.0)],
                backend="stub",
            )

        def stats(self) -> dict:
            return {"backend": "stub", "corpus_entries": 0, "rules": 0}

        def defer_while(self, _predicate) -> None:  # pragma: no cover - unused
            return None

    config = AppConfig.load()
    config.translation["nmt_model"] = None
    # Change detection is switched off for this test, and that is deliberate rather than
    # convenient. The detector compares whole-frame means, so it does not notice a
    # one-character difference -- which is exactly the difference OCR produces between
    # frames. Left on, the second frame would never reach OCR and this test would be
    # measuring the detector instead of the reuse.
    #
    # That interaction is worth knowing about on its own: a one-character change is
    # invisible to the detector, so the reuse layer only comes into play for text
    # changes the detector *did* notice.
    config.capture["diff_threshold"] = 0.0
    capturer = SyntheticCapturer(
        frames=[("the void realm holds firm",), ("the void realm holds firm.",)],
        hold_seconds=0.8,
    )
    translator = CountingTranslator()
    session = Session(config, capturer=capturer, translator=translator)
    session.build()
    channel = session.subscribe()
    session.start()
    deadline = time.perf_counter() + 4.0
    seen = 0
    while time.perf_counter() < deadline:
        try:
            event = channel.get(timeout=0.1)
        except Exception:
            continue
        if event.get("type") == "subtitle":
            seen += 1
    session.stop()

    check.check("the session produced subtitles to compare", seen > 0, f"{seen} event(s)")
    check.check(
        "both frames were processed, so the reuse below is not an artefact of a drop",
        seen >= 2,
        f"{seen} subtitle event(s); one per frame is expected",
    )
    check.check(
        "the translator was asked to translate the line only once",
        len(translator.calls) == 1,
        f"calls={translator.calls}",
    )
    check.check(
        "and the pipeline counted the reuse",
        session.pipeline.reused_lines >= 1,
        f"reused_lines={session.pipeline.reused_lines}, "
        f"dedup_hits={session.pipeline.recent.hits}",
    )
    stats = session.pipeline.to_dict()
    check.check(
        "the counters reach the stats output, so the saving is visible",
        "reused_lines" in stats and "dedup_hit_rate" in stats,
        f"reused_lines={stats.get('reused_lines')}",
    )

    # ---------------------------------------------------------------- #
    check.section("END TO END: an untranslatable pair is not shown as a translation")
    # The shipped corpus and rules are en<->zh only, so asking for zh->ja through the
    # corpus path returns the Chinese unchanged. Showing that as the translation is
    # worse than showing nothing: the user reads their own language back and concludes
    # the translator is broken. It must be reported as what it is.
    zh_config = AppConfig.load()
    zh_config.translation["nmt_model"] = None
    zh_config.translation["target"] = "ja"
    zh_config.capture["diff_threshold"] = 0.0
    zh_session = Session(
        zh_config,
        capturer=SyntheticCapturer(frames=[("他突破到了虚空境界",)], hold_seconds=99),
    )
    zh_session.build()
    zh_channel = zh_session.subscribe()
    zh_session.start()
    deadline = time.perf_counter() + 3.0
    payload = None
    while time.perf_counter() < deadline:
        try:
            event = zh_channel.get(timeout=0.1)
        except Exception:
            continue
        if event.get("type") == "subtitle":
            payload = event.get("data") or {}
            break
    zh_session.stop()

    check.check("a Chinese frame reached the overlay", payload is not None)
    if payload:
        lines = payload.get("lines") or [{}]
        line = lines[0]
        check.check(
            "the text came back unchanged, which is the situation under test",
            (line.get("target") or "").strip() == "他突破到了虚空境界",
            f"target={line.get('target')!r}",
        )
        check.check(
            "and it is reported with zero coverage rather than as a real translation",
            float(line.get("coverage", 1.0)) == 0.0,
            f"coverage={line.get('coverage')} -- zero is what makes the overlay dim it",
        )
        check.check(
            "the pipeline counted it as untranslated",
            zh_session.pipeline.untranslated_lines >= 1,
            f"untranslated_lines={zh_session.pipeline.untranslated_lines}",
        )
        check.check(
            "so a missing language pair shows up as a number, not as silent text",
            zh_session.pipeline.to_dict().get("untranslated_lines", 0) >= 1,
        )

    return check.report()


if __name__ == "__main__":
    raise SystemExit(main())
