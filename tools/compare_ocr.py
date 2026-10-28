#!/usr/bin/env python3
"""Compare the C++ OCR against the Python reference on identical pixels.

    .venv/Scripts/python.exe tools/compare_ocr.py \
        --manifest build/w11_fixtures/manifest.json \
        --reference build/w11_fixtures/reference.jsonl \
        --det .venv/Lib/site-packages/rapidocr_onnxruntime/models/ch_PP-OCRv4_det_infer.onnx \
        --rec .venv/Lib/site-packages/rapidocr_onnxruntime/models/ch_PP-OCRv4_rec_infer.onnx \
        --chars spike/chars.txt \
        --screen-manifest build/w11_screens/manifest.json \
        --screen-reference build/w11_screens/reference.jsonl

Neither fixture set is committed: ``tests/fixtures/make_ocr_fixtures.py`` produces
the rendered frames, ``tests/fixtures/capture_screens.py`` captures the real
ones and records the Python reference for both. Without ``--screen-manifest``
this prints "screens: not verified" rather than passing on fixtures alone.

What it measures, and why each gate exists:

* **normalised text agreement** -- punctuation and whitespace differ across
  engines for reasons that do not matter to a subtitle; what must match is the
  characters. 0.95 for the rendered fixtures, 0.90 for real screenshots.
* **box count equality and max coordinate error** -- a subtitle that lands in
  the wrong place is a defect even when the words are right. 3 px.
* **latency P50** -- the C++ port must not be slower than the Python baseline by
  more than 20%, and never above 120 ms for a 1200x200 frame.

The CLI is run twice: once with every fixture frame (one model load), and once
on the timing frame with --repeat so the percentiles exclude model loading.

Exit code 0 only when every enabled gate passes. If real screenshots were not
captured, this prints "not verified" instead of silently skipping: a green run
that never looked at a screen must not be indistinguishable from one that did.
"""

from __future__ import annotations

import argparse
import difflib
import json
import math
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_CLI = ROOT / "build-w11" / "Release" / "watashi_cli.exe"
BASELINE = ROOT / "bench" / "baseline.json"

SYNTHETIC_MIN_AGREEMENT = 0.95
SCREEN_MIN_AGREEMENT = 0.90
MAX_BOX_ERROR_PX = 3
MAX_P50_MS = 120.0
BASELINE_TOLERANCE = 1.2  # "no worse than 20%"


def normalise(text: str) -> str:
    """Drop whitespace and punctuation; the comparison basis everywhere."""
    return "".join(
        char
        for char in text
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, int(math.ceil(percent / 100.0 * len(ordered))) - 1))
    return ordered[index]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            records.append(json.loads(raw))
    return records


def sort_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(line: dict[str, Any]) -> tuple[float, float]:
        box = line.get("box") or [0, 0, 0, 0]
        return (float(box[1]), float(box[0]))

    return sorted(lines, key=key)


def compare_frames(
    cpp: dict[str, list[dict[str, Any]]],
    reference: dict[str, list[dict[str, Any]]],
    order: list[str],
) -> dict[str, Any]:
    joined_cpp: list[str] = []
    joined_ref: list[str] = []
    per_frame: list[dict[str, Any]] = []
    box_error = 0
    count_mismatches = 0

    for name in order:
        cpp_lines = sort_lines(cpp.get(name, []))
        ref_lines = sort_lines(reference.get(name, []))
        cpp_text = "\n".join(normalise(line.get("text", "")) for line in cpp_lines)
        ref_text = "\n".join(normalise(line.get("text", "")) for line in ref_lines)
        agreement = (
            1.0
            if cpp_text == ref_text
            else difflib.SequenceMatcher(None, cpp_text, ref_text).ratio()
        )
        joined_cpp.append(cpp_text)
        joined_ref.append(ref_text)

        frame_error = 0
        if len(cpp_lines) != len(ref_lines):
            count_mismatches += 1
        else:
            for cpp_line, ref_line in zip(cpp_lines, ref_lines):
                cpp_box = cpp_line.get("box") or [0, 0, 0, 0]
                ref_box = ref_line.get("box") or [0, 0, 0, 0]
                frame_error = max(
                    frame_error,
                    max(abs(int(a) - int(b)) for a, b in zip(cpp_box, ref_box)),
                )
            box_error = max(box_error, frame_error)

        per_frame.append(
            {
                "frame": name,
                "agreement": round(agreement, 4),
                "cpp_lines": len(cpp_lines),
                "reference_lines": len(ref_lines),
                "box_error_px": frame_error if len(cpp_lines) == len(ref_lines) else None,
            }
        )

    overall = difflib.SequenceMatcher(
        None, "\n".join(joined_cpp), "\n".join(joined_ref)
    ).ratio()
    return {
        "frames": len(order),
        "agreement": round(overall, 4),
        "min_frame_agreement": round(min((item["agreement"] for item in per_frame), default=0.0), 4),
        "box_count_mismatches": count_mismatches,
        "max_box_error_px": box_error,
        "per_frame": per_frame,
    }


def run_cli(
    cli: Path,
    frames: list[Path],
    det: str,
    rec: str,
    chars: str,
    repeat: int,
) -> tuple[list[dict[str, Any]], list[float]]:
    command = [
        str(cli),
        "--ocr",
        "onnx",
        "--det",
        det,
        "--rec",
        rec,
        "--chars",
        chars,
        "--repeat",
        str(repeat),
    ]
    for frame in frames:
        command.extend(["--frame", str(frame)])

    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    if completed.returncode != 0:
        raise SystemExit(
            f"watashi_cli exited {completed.returncode}:\n{completed.stderr.strip()}"
        )
    records = [json.loads(raw) for raw in completed.stdout.splitlines() if raw.strip()]
    timings = [
        float(raw.split("=", 1)[1])
        for raw in completed.stderr.splitlines()
        if raw.startswith("run_ms=")
    ]
    return records, timings


def baseline_limit() -> tuple[float, float | None]:
    """Returns (hard cap, baseline-relative cap when a baseline exists)."""
    if not BASELINE.exists():
        return MAX_P50_MS, None
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    p50 = float((data.get("metrics") or {}).get("ocr_ms", {}).get("p50", 0.0))
    return MAX_P50_MS, (p50 * BASELINE_TOLERANCE if p50 > 0 else None)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--det", required=True)
    parser.add_argument("--rec", required=True)
    parser.add_argument("--chars", required=True)
    parser.add_argument("--timing-frame", type=Path, default=None)
    parser.add_argument("--repeat", type=int, default=20)
    parser.add_argument("--screen-manifest", type=Path, default=None)
    parser.add_argument("--screen-reference", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.cli.exists():
        raise SystemExit(f"missing {args.cli}; build first")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    base = args.manifest.parent
    order = [item["file"] for item in manifest["frames"]]
    frame_paths = [base / name for name in order]

    reference = {item["frame"]: item["lines"] for item in read_jsonl(args.reference)}
    records, _ = run_cli(args.cli, frame_paths, args.det, args.rec, args.chars, 1)
    cpp = {
        Path(record["frame"]).name: record["lines"]
        for record in records
    }

    result = compare_frames(cpp, reference, order)
    failures: list[str] = []

    print(f"fixtures: {result['frames']} frame(s)")
    print(f"  normalised agreement : {result['agreement']:.4f} "
          f"(min per frame {result['min_frame_agreement']:.4f})")
    print(f"  box count mismatches : {result['box_count_mismatches']}")
    print(f"  max box error        : {result['max_box_error_px']} px")
    if result["agreement"] < SYNTHETIC_MIN_AGREEMENT:
        failures.append(
            f"synthetic agreement {result['agreement']:.4f} < {SYNTHETIC_MIN_AGREEMENT}"
        )
    if result["min_frame_agreement"] < SYNTHETIC_MIN_AGREEMENT:
        failures.append(
            f"a frame fell to {result['min_frame_agreement']:.4f} < {SYNTHETIC_MIN_AGREEMENT}"
        )
    if result["box_count_mismatches"]:
        failures.append(f"{result['box_count_mismatches']} frame(s) disagree on line count")
    if result["max_box_error_px"] > MAX_BOX_ERROR_PX:
        failures.append(
            f"max box error {result['max_box_error_px']} px > {MAX_BOX_ERROR_PX}"
        )

    timing_frame = args.timing_frame or frame_paths[0]
    _, timings = run_cli(
        args.cli, [timing_frame], args.det, args.rec, args.chars, args.repeat
    )
    p50 = percentile(timings, 50)
    p95 = percentile(timings, 95)
    hard_cap, relative_cap = baseline_limit()
    print(f"latency: {len(timings)} pass(es) on {timing_frame.name}")
    print(f"  P50 {p50:.2f} ms / P95 {p95:.2f} ms "
          f"(cap {hard_cap:.0f} ms" + (f", baseline x1.2 {relative_cap:.2f} ms" if relative_cap else "") + ")")
    if p50 > hard_cap:
        failures.append(f"P50 {p50:.2f} ms > {hard_cap:.0f} ms")
    if relative_cap is not None and p50 > relative_cap:
        failures.append(
            f"P50 {p50:.2f} ms is more than 20% worse than the baseline ({relative_cap:.2f} ms)"
        )

    if args.screen_manifest and args.screen_reference:
        screen_manifest = json.loads(args.screen_manifest.read_text(encoding="utf-8"))
        screen_base = args.screen_manifest.parent
        screen_order = [item["file"] for item in screen_manifest["frames"]]
        screen_reference = {
            item["frame"]: item["lines"] for item in read_jsonl(args.screen_reference)
        }
        screen_records, _ = run_cli(
            args.cli,
            [screen_base / name for name in screen_order],
            args.det,
            args.rec,
            args.chars,
            1,
        )
        screen_cpp = {Path(record["frame"]).name: record["lines"] for record in screen_records}
        screen = compare_frames(screen_cpp, screen_reference, screen_order)
        print(f"screens: {screen['frames']} frame(s)")
        print(f"  normalised agreement : {screen['agreement']:.4f}")
        # Real screens have many small lines whose top-left corners tie, so
        # pairing them by position is not stable. Reported for context only; the
        # screen gate is the agreement above.
        print(f"  max box error        : {screen['max_box_error_px']} px (informational)")
        if screen["agreement"] < SCREEN_MIN_AGREEMENT:
            failures.append(
                f"screen agreement {screen['agreement']:.4f} < {SCREEN_MIN_AGREEMENT}"
            )
    else:
        print("screens: not verified (no screen fixture set supplied)")

    if failures:
        print("FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
