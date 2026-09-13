#!/usr/bin/env python3
"""OCR stage-switch verification. Headless apart from the engine itself.

These assertions exist because the same ground was covered five times by throwaway
probes that each reached a confident wrong answer, and nothing prevented a sixth. What
they pin down:

* ``use_det`` / ``use_rec`` are honoured, and the way to change them is ``_kwargs`` --
  the snapshot taken in ``__init__`` and passed on every call. Mutating ``obj.use_rec``
  looks like it should work and does nothing; three probes compared identical
  configurations because of it.
* a detection-only result does not crash the recogniser. It used to: ``_unpack``
  assumed ``(box, text, score)`` and a detection-only run returns bare quads, so
  ``float(score)`` raised ``TypeError`` and took the whole call with it.
* the normal path still returns text, so the above are not passing because recognition
  is broken in general.

Timing is deliberately NOT asserted here. The recognition stage is about 78% of OCR
cost and the detection stage about 22%, measured with interleaved sampling; that lives
in ``bench_ocr_stages.py`` because a timing threshold in CI fails on a busy runner and
teaches people to ignore the suite.

    run.cmd selfcheck_ocr --summary
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from watashi.checks import Checker  # noqa: E402

from watashi.config import AppConfig
from watashi.session import build_ocr


def make_frame(lines: int = 3) -> np.ndarray:
    image = Image.new("RGB", (900, 200), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 24)
    except OSError:
        font = ImageFont.load_default()
    for index in range(lines):
        draw.text((20, 20 + index * 56), f"sample line number {index + 1}",
                  fill="black", font=font)
    return np.asarray(image, dtype=np.uint8)[:, :, ::-1].copy()


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("OCR stage switches (loads the OCR engine; no screen, no model)")
    print("=" * 78)

    engine = build_ocr(AppConfig.load())
    engine.ensure_loaded()
    frame = make_frame(3)
    base = dict(getattr(engine, "_kwargs", {}) or {})

    check.section("the stage switches reach RapidOCR")
    check.check(
        "the call-time kwargs name all three stages",
        {"use_det", "use_rec", "use_cls"} <= set(base),
        f"_kwargs={base}",
    )

    engine._kwargs = base  # noqa: SLF001
    normal = engine.recognize(frame)
    with_text = [line for line in normal.lines if line.text.strip()]
    check.check(
        "the baseline reads the text (so the checks below are not vacuous)",
        len(with_text) >= 2,
        f"{len(normal.lines)} box(es), {len(with_text)} with text",
    )

    # ---------------------------------------------------------------- #
    check.section("switching recognition off does not crash the recogniser")
    # The bug: _unpack assumed (box, text, score), but a detection-only run returns
    # bare quads, so float(point) raised TypeError and the whole call failed. The
    # honest answer for a function whose contract is (box, text, score) is to skip
    # entries that are not that shape.
    engine._kwargs = {**base, "use_rec": False}  # noqa: SLF001
    try:
        detected = engine.recognize(frame)
        raised = None
    except Exception as exc:  # pragma: no cover - the regression under test
        detected = None
        raised = exc
    check.check(
        "recognize() with use_rec=False returns instead of raising",
        raised is None,
        f"raised {type(raised).__name__}: {raised}" if raised else "returned",
    )
    if detected is not None:
        check.check(
            "and it reports no text, because detection carries none",
            not any(line.text.strip() for line in detected.lines),
            f"{len(detected.lines)} box(es) with no text",
        )
        # Detection is what it can still do: the boxes are found even though the text
        # is not read. Asserted through _invoke because recognize()'s contract is text.
        boxes = engine._invoke(frame)  # noqa: SLF001
        found = len(boxes[0]) if isinstance(boxes, (list, tuple)) and boxes and boxes[0] else 0
        check.check(
            "while still detecting the boxes it found before",
            found >= 2,
            f"_invoke reported {found} box(es) for {len(with_text)} line(s) of text",
        )

    engine._kwargs = {**base, "use_det": False}  # noqa: SLF001
    no_det = engine.recognize(frame)
    check.check(
        "switching detection off finds nothing at all",
        len(no_det.lines) == 0,
        f"{len(no_det.lines)} line(s)",
    )

    # ---------------------------------------------------------------- #
    check.section("the trap that cost five attempts to find")
    # `_kwargs` is snapshotted in __init__ and passed on every call. Setting the
    # attribute afterwards changes nothing, which is exactly why three probes compared
    # identical configurations and reported "the flags are inert".
    engine._kwargs = base  # noqa: SLF001
    snapshot = dict(engine._kwargs)  # noqa: SLF001
    engine.use_rec = False
    check.check(
        "mutating the attribute does not touch the call-time kwargs",
        engine._kwargs.get("use_rec") is True,  # noqa: SLF001
        "so any change must go through _kwargs, and the attribute is a decoy",
    )
    engine.use_rec = True

    # ---------------------------------------------------------------- #
    check.section("_unpack tolerates the detection-only shape directly")
    # Unit level, so this holds even if the engine cannot be loaded elsewhere.
    from watashi.ocr import RapidOcrEngine

    quad = [[0.0, 0.0], [10.0, 0.0], [10.0, 5.0], [0.0, 5.0]]
    unpacked = RapidOcrEngine._unpack(([quad, quad], 0.01))  # noqa: SLF001
    check.check(
        "bare quads are skipped rather than crashing",
        unpacked == [],
        f"got {unpacked}",
    )
    unpacked = RapidOcrEngine._unpack(([(quad, "hello", 0.9)], 0.01))  # noqa: SLF001
    check.check(
        "a real (box, text, score) entry still comes through",
        len(unpacked) == 1 and unpacked[0][1] == "hello",
        f"got {unpacked}",
    )
    unpacked = RapidOcrEngine._unpack(([(quad, "hello", None)], 0.01))  # noqa: SLF001
    check.check(
        "a non-numeric score is skipped, not coerced",
        unpacked == [],
        f"got {unpacked}",
    )

    engine._kwargs = base  # noqa: SLF001

    # ---------------------------------------------------------------- #
    check.section("a configured cap really limits what gets translated")
    # The setting caps how many recognised boxes are translated and drawn. Asserted
    # through a real session rather than by calling the helper, because the value of
    # the setting is entirely in whether the rest of the pipeline honours it.
    from watashi.session import Session
    from watashi.synth import SyntheticCapturer

    config = AppConfig.load()
    config.translation["nmt_model"] = None
    config.capture["diff_threshold"] = 0.0
    config.ocr["max_boxes"] = 2
    lines = ("first line of the screen", "second line of the screen",
             "third line of the screen", "fourth line of the screen",
             "fifth line of the screen")
    capped = Session(config, capturer=SyntheticCapturer(frames=[lines], hold_seconds=99))
    capped.build()
    channel = capped.subscribe()
    capped.start()
    deadline = time.perf_counter() + 3.0
    payload = None
    while time.perf_counter() < deadline:
        try:
            event = channel.get(timeout=0.1)
        except Exception:
            continue
        if event.get("type") == "subtitle":
            payload = event.get("data") or {}
            break
    capped.stop()

    check.check("the capped session produced a frame", payload is not None)
    if payload:
        emitted = payload.get("lines") or []
        check.check(
            "no more lines are translated than the cap allows",
            len(emitted) <= 2,
            f"cap=2, emitted={len(emitted)}",
        )
        check.check(
            "the cap is visible as a counter rather than silently changing the screen",
            capped.pipeline.capped_lines >= 1,
            f"capped_lines={capped.pipeline.capped_lines}",
        )
        ys = [line.get("box", [0, 0, 0, 0])[1] for line in emitted if line.get("box")]
        check.check(
            "reading order is preserved, so the cap never reorders the display",
            ys == sorted(ys),
            f"y positions={ys}",
        )
        check.check(
            "the counter reaches the stats output",
            capped.pipeline.to_dict().get("capped_lines", 0) >= 1,
        )

    # A cap of 0 must mean "no cap", not "translate nothing".
    config.ocr["max_boxes"] = 0
    uncapped = Session(config, capturer=SyntheticCapturer(frames=[lines], hold_seconds=99))
    uncapped.build()
    channel = uncapped.subscribe()
    uncapped.start()
    deadline = time.perf_counter() + 3.0
    payload = None
    while time.perf_counter() < deadline:
        try:
            event = channel.get(timeout=0.1)
        except Exception:
            continue
        if event.get("type") == "subtitle":
            payload = event.get("data") or {}
            break
    uncapped.stop()
    check.check(
        "0 means no cap, rather than translating nothing at all",
        payload is not None and len(payload.get("lines") or []) > 2,
        f"{len((payload or {}).get('lines') or [])} line(s) with the cap off",
    )
    check.check(
        "and nothing was counted as capped",
        uncapped.pipeline.capped_lines == 0,
        f"capped_lines={uncapped.pipeline.capped_lines}",
    )

    return check.report()


if __name__ == "__main__":
    raise SystemExit(main())
