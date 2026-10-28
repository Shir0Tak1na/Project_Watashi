#!/usr/bin/env python3
"""Capture 10 real screen strips and the Python reference for them.

    .venv/Scripts/python.exe tests/fixtures/capture_screens.py

Outputs under build/ (never in git):

    build/w11_screens/screen_00.bgr ... screen_09.bgr   1200x200x3 raw BGR
    build/w11_screens/manifest.json
    build/w11_screens/reference.jsonl

Only the bottom 200 rows of the primary monitor are kept, resized to the
1200x200 region the engine works in. That is enough to exercise real fonts,
subpixel rendering and whatever happens to be on screen, while keeping the
capture to a strip rather than a whole desktop.

If there is no desktop session (mss cannot open a capture), this exits 3 with
a clear message. tools/compare_ocr.py then reports "not verified" rather than a
green run that never touched a screen.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
PROTOTYPE = ROOT / "prototype"
OUT = ROOT / "build" / "w11_screens"

WIDTH = 1200
HEIGHT = 200


def load_ocr():
    sys.path.insert(0, str(PROTOTYPE))
    from watashi.config import AppConfig
    from watashi.session import build_ocr

    return build_ocr(AppConfig.load())


def reference_for(engine, frame: np.ndarray) -> list[dict]:
    result = engine.recognize(frame)
    return [
        {
            "text": line.text,
            "box": [int(value) for value in line.box],
            "score": round(float(line.confidence), 6),
        }
        for line in result.lines
    ]


def grab_strip(sct, monitor: dict) -> np.ndarray:
    shot = np.array(sct.grab(monitor))  # BGRA
    bgr = np.ascontiguousarray(shot[:, :, :3])
    scale = WIDTH / float(bgr.shape[1])
    resized = cv2.resize(
        bgr, (WIDTH, max(1, int(round(bgr.shape[0] * scale)))), interpolation=cv2.INTER_AREA
    )
    strip = resized[max(0, resized.shape[0] - HEIGHT) :, :]
    if strip.shape[0] < HEIGHT:
        padded = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        padded[HEIGHT - strip.shape[0] :, :] = strip
        strip = padded
    return strip


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--interval", type=float, default=0.4)
    args = parser.parse_args(argv)

    try:
        import mss
    except ImportError:
        print("mss is not installed; cannot capture a real screen", file=sys.stderr)
        return 3

    OUT.mkdir(parents=True, exist_ok=True)
    engine = load_ocr()

    try:
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            manifest = {"width": WIDTH, "height": HEIGHT, "count": args.count, "frames": []}
            references = []
            for index in range(args.count):
                strip = grab_strip(sct, monitor)
                name = f"screen_{index:02d}.bgr"
                np.ascontiguousarray(strip).tofile(OUT / name)
                manifest["frames"].append({"file": name})
                references.append({"frame": name, "lines": reference_for(engine, strip)})
                time.sleep(max(0.0, args.interval))
    except Exception as error:  # noqa: BLE001 - no display is a legitimate outcome
        print(f"screen capture unavailable: {type(error).__name__}: {error}", file=sys.stderr)
        print("not verified: real screen", file=sys.stderr)
        return 3

    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (OUT / "reference.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for record in references:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    detected = sum(1 for item in references if item["lines"])
    print(f"wrote {args.count} screen strip(s) to {OUT} "
          f"({detected} with recognised text)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
