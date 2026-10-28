#!/usr/bin/env python3
"""One command that rebuilds both OCR fixture sets and runs the full comparison.

    .venv/Scripts/python.exe tools/check_ocr.py \
        --det .venv/Lib/site-packages/rapidocr_onnxruntime/models/ch_PP-OCRv4_det_infer.onnx \
        --rec .venv/Lib/site-packages/rapidocr_onnxruntime/models/ch_PP-OCRv4_rec_infer.onnx \
        --chars spike/chars.txt

Steps, all of which write into build/ and none of which touch git:

1. ``tests/fixtures/make_ocr_fixtures.py`` re-renders the 50 synthetic frames
   (fixed seed, so they are the same pixels every run) and records the Python
   reference for them.
2. ``tests/fixtures/capture_screens.py`` captures ``--screen-count`` real screen
   strips and records their Python reference. Screen content changes between
   runs by nature; that is why the pixels are not committed.
3. ``tools/compare_ocr.py`` runs the CLI over both sets and applies the W1.1
   gates (agreement, box count, box error, P50).

Without ``--screen-count 0`` the screen set is always recaptured, so the number
in an acceptance record can be reproduced by re-running this file rather than
being a claim about an old directory.

Exit codes: 0 all gates pass, 1 a gate failed, 3 no display (screen comparison
not verified), 2 setup problem.
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
DEFAULT_CLI = ROOT / "build-w11" / "Release" / "watashi_cli.exe"
REPORT = ROOT / "build" / "w11_check.json"


def run(script: Path, args: list[str]) -> int:
    command = [sys.executable, str(script), *args]
    print(f"\n> {' '.join(command)}")
    completed = subprocess.run(command)
    return completed.returncode


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    parser.add_argument("--det", required=True)
    parser.add_argument("--rec", required=True)
    parser.add_argument("--chars", required=True)
    parser.add_argument("--repeat", type=int, default=20)
    parser.add_argument("--screen-count", type=int, default=10)
    args = parser.parse_args(argv)

    if not args.cli.exists():
        print(f"missing {args.cli}; build first", file=sys.stderr)
        return 2

    fixture_code = run(FIXTURES / "make_ocr_fixtures.py", [])
    if fixture_code != 0:
        return 2

    screen_code = 0
    if args.screen_count > 0:
        screen_code = run(
            FIXTURES / "capture_screens.py", ["--count", str(args.screen_count)]
        )

    compare_args = [
        "--cli", str(args.cli),
        "--manifest", str(ROOT / "build" / "w11_fixtures" / "manifest.json"),
        "--reference", str(ROOT / "build" / "w11_fixtures" / "reference.jsonl"),
        "--det", args.det,
        "--rec", args.rec,
        "--chars", args.chars,
        "--repeat", str(args.repeat),
    ]
    if screen_code == 0 and args.screen_count > 0:
        compare_args.extend(
            [
                "--screen-manifest", str(ROOT / "build" / "w11_screens" / "manifest.json"),
                "--screen-reference", str(ROOT / "build" / "w11_screens" / "reference.jsonl"),
            ]
        )
    elif args.screen_count > 0:
        print("\nnot verified: real screen (capture failed with exit "
              f"{screen_code})", file=sys.stderr)

    compare_code = run(ROOT / "tools" / "compare_ocr.py", compare_args)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(
            {
                "recorded": datetime.date.today().isoformat(),
                "cli": str(args.cli),
                "fixtures_exit": fixture_code,
                "screen_exit": screen_code,
                "compare_exit": compare_code,
                "screens_verified": screen_code == 0 and args.screen_count > 0,
                "ok": compare_code == 0,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if compare_code == 0:
        return 0
    return 3 if screen_code != 0 and args.screen_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
