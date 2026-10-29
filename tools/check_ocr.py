#!/usr/bin/env python3
"""One command that rebuilds both OCR fixture sets and runs the full comparison.

    .venv/Scripts/python.exe tools/check_ocr.py \
        --det .venv/Lib/site-packages/rapidocr_onnxruntime/models/ch_PP-OCRv4_det_infer.onnx \
        --rec .venv/Lib/site-packages/rapidocr_onnxruntime/models/ch_PP-OCRv4_rec_infer.onnx \
        --chars spike/chars.txt

Output directory: ``--out`` if given; otherwise the first writable of ``build/``
and ``build-w11/``. That matters because some interpreters here cannot write
``build/`` (PermissionError) while ``build-w11/`` is fine, and a check that only
runs as root is not a check anyone can reproduce.

Steps, all inside that output directory and none of them touching git:

1. ``tests/fixtures/make_ocr_fixtures.py`` re-renders the 50 synthetic frames
   (fixed seed, so the same pixels every run) and records the Python reference.
2. ``tests/fixtures/capture_subtitles.py`` writes a fixed subtitle clip, plays
   it in a controlled borderless window and captures the strip with mss.
   Screen content is controlled, not "whatever was on the desktop".
3. ``tools/compare_ocr.py`` runs the CLI over both sets and applies the gates
   (per-frame mean agreement, box count, box error, P50).

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


def run(script: Path, args: list[str]) -> int:
    command = [sys.executable, str(script), *args]
    print(f"\n> {' '.join(command)}")
    return subprocess.run(command).returncode


def writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def choose_out(requested: Path | None) -> Path:
    if requested is not None:
        return requested
    for candidate in (ROOT / "build", ROOT / "build-w11"):
        if writable(candidate):
            return candidate
    raise SystemExit("no writable output directory; pass --out")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    parser.add_argument("--det", required=True)
    parser.add_argument("--rec", required=True)
    parser.add_argument("--chars", required=True)
    parser.add_argument("--repeat", type=int, default=20)
    parser.add_argument("--screen-count", type=int, default=10)
    parser.add_argument("--out", type=Path, default=None,
                        help="output directory (default: first writable of build/, build-w11/)")
    args = parser.parse_args(argv)

    if not args.cli.exists():
        print(f"missing {args.cli}; build first", file=sys.stderr)
        return 2

    out = choose_out(args.out)
    print(f"output directory: {out}")
    fixtures_dir = out / "w11_fixtures"
    screens_dir = out / "w11_subtitles"

    fixture_code = run(FIXTURES / "make_ocr_fixtures.py", ["--out", str(fixtures_dir)])
    if fixture_code != 0:
        return 2

    screen_code = 0
    if args.screen_count > 0:
        screen_code = run(
            FIXTURES / "capture_subtitles.py",
            ["--out", str(screens_dir), "--count", str(args.screen_count)],
        )

    compare_args = [
        "--cli", str(args.cli),
        "--manifest", str(fixtures_dir / "manifest.json"),
        "--reference", str(fixtures_dir / "reference.jsonl"),
        "--det", args.det,
        "--rec", args.rec,
        "--chars", args.chars,
        "--repeat", str(args.repeat),
    ]
    if screen_code == 0 and args.screen_count > 0:
        compare_args.extend(
            [
                "--screen-manifest", str(screens_dir / "manifest.json"),
                "--screen-reference", str(screens_dir / "reference.jsonl"),
            ]
        )
    elif args.screen_count > 0:
        print("\nnot verified: controlled subtitle strip (capture exited "
              f"{screen_code})", file=sys.stderr)

    compare_code = run(ROOT / "tools" / "compare_ocr.py", compare_args)

    report = out / "w11_check.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            {
                "recorded": datetime.date.today().isoformat(),
                "cli": str(args.cli),
                "out": str(out),
                "fixtures_exit": fixture_code,
                "screen_exit": screen_code,
                "compare_exit": compare_code,
                "screens_verified": screen_code == 0 and args.screen_count > 0,
                "screen_kind": "controlled-subtitles",
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
