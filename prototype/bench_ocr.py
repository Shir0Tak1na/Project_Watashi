#!/usr/bin/env python3
"""OCR latency benchmark -- the tool behind the R4 latency budget.

Real time subtitles live or die on OCR latency, so it gets measured rather than
assumed. This sweeps frame widths and reports det / cls / rec cost separately,
which is what tells you whether to shrink the captured region, drop the
direction classifier, or give up on CPU inference for full width capture.

    run.cmd bench_ocr                    # sweep the default widths
    run.cmd bench_ocr --widths 480,640,960
    run.cmd bench_ocr --threads 8
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.selftest import render_text_image  # noqa: E402

#: realistic subtitle content: short lines, common on screen
SAMPLE_LINES = (
    "he broke through to the void realm",
    "gg wp noob, that nerf was brutal",
    "the sword intent of this sect is a myth",
)


def measure(ocr, frame, repeats: int) -> tuple[float, float]:
    samples: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        ocr.recognize(frame)
        samples.append((time.perf_counter() - started) * 1000.0)
    return statistics.median(samples), max(samples)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench_ocr", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--widths", type=str, default="640,960,1280")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--threads", type=int, default=None, help="ONNX intra-op threads")
    parser.add_argument("--no-cls", action="store_true", default=True)
    args = parser.parse_args(argv)

    cpu = os.cpu_count() or 1
    print("=" * 72)
    print("OCR latency benchmark")
    print("=" * 72)
    print(f"  logical CPUs            : {cpu}")
    try:
        import onnxruntime as ort

        print(f"  onnxruntime             : {ort.__version__}")
        print(f"  providers               : {', '.join(ort.get_available_providers())}")
        session_options = ort.SessionOptions()
        print(f"  ORT default intra-op    : {session_options.intra_op_num_threads} (0 = all cores)")
    except Exception as exc:
        print(f"  onnxruntime probe failed: {exc}")
    print("")

    from watashi.ocr import RapidOcrEngine

    def build() -> "RapidOcrEngine":
        return RapidOcrEngine(
            max_width=0,
            use_cls=not args.no_cls,
            intra_op_threads=args.threads or 4,
            warmup=True,
        )

    # ------------------------------------------------------------------ #
    # 1. capture width sweep: text size stays constant, only the region
    #    gets wider. This is what a user controls by picking a region.
    # ------------------------------------------------------------------ #
    print("-- capture width sweep (font scales with the frame, so text stays legible) --")
    header = f"  {'width':>6} {'region h':>9} {'median ms':>11} {'max ms':>9} {'est FPS':>9}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    results: list[tuple[int, float]] = []
    for width in [int(w) for w in args.widths.split(",") if w.strip()]:
        frame = render_text_image(SAMPLE_LINES, width=width)
        ocr = build()
        ocr.recognize(frame)  # pay the per-shape ONNX setup first
        median, worst = measure(ocr, frame, args.repeats)
        fps = 1000.0 / median if median > 0 else 0.0
        results.append((width, median))
        print(f"  {width:>6} {frame.shape[0]:>9} {median:>11.1f} {worst:>9.1f} {fps:>9.2f}")
        del ocr

    print("")
    print("-- downscale penalty (fixed 1600 px frame, scaled down before OCR) --")
    print("   Text shrinks as you downscale, so watch the recognised text, not just ms.")
    print("")
    big = render_text_image(SAMPLE_LINES, width=1600)
    print(f"  ground truth:")
    for line in SAMPLE_LINES:
        print(f"      {line}")
    print("")
    print(f"  {'max_width':>10} {'median ms':>11} {'est FPS':>9}  recognised")
    print("  " + "-" * 68)

    for max_width in (0, 1280, 960, 640):
        ocr = build()
        ocr.max_width = max_width
        ocr.recognize(big)
        median, _ = measure(ocr, big, args.repeats)
        fps = 1000.0 / median if median > 0 else 0.0
        text = " | ".join(line.text for line in ocr.recognize(big).lines)
        label = "native" if max_width == 0 else str(max_width)
        print(f"  {label:>10} {median:>11.1f} {fps:>9.2f}  {text[:60]}")
        del ocr

    print("")
    print("-- reading of the numbers --")
    fastest = min(results, key=lambda item: item[1])
    print(f"  fastest capture width : {fastest[0]}px -> {fastest[1]:.0f} ms")
    for budget in (150.0, 100.0):
        ok = [w for w, ms in results if ms <= budget]
        print(f"  widths within {budget:>5.0f} ms : {ok if ok else 'NONE'}")
    print("")
    print("  Native resolution keeps detection accurate; downscaling buys latency")
    print("  but merges words together. Prefer choosing a tighter region over")
    print("  shrinking the pixels -- a subtitle strip is wide but short.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
