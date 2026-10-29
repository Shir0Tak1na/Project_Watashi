#!/usr/bin/env python3
"""Play a fixed subtitle clip in a borderless window and capture its strip.

    .venv/Scripts/python.exe tests/fixtures/capture_subtitles.py --out build/w11_subtitles

Why not just photograph the desktop: a taskbar is not a subtitle. The previous
screen set captured whatever happened to be at the bottom of the monitor, so it
mixed a clock, a date and two dock labels into "lines" and made the 0.90 gate
measure desktop layout rather than subtitle OCR.

This fixture is controlled and still real screen pixels:

1. ten subtitle frames are rendered and written to an mp4 in the output dir;
2. the mp4 is decoded and played in a borderless, always-on-top window at a
   fixed position, sized 1200x200 at 1:1;
3. mss captures exactly that rectangle from the compositor;
4. every capture is checked against the displayed video frame, so a capture of
   the wrong window fails loudly instead of producing plausible nonsense;
5. the Python reference is computed on the *captured* pixels, so both engines
   read the same bytes;
6. ``--hold`` keeps the window on screen after recording, and ``--loop`` replays
   the clip while it holds, which is how a live ``--capture window`` run gets
   both a still screen and a changing one. ``--title`` names the window so
   ``--window-title`` can find it.

Nothing here is committed: the mp4, frames and reference live under the output
directory (build/ by default), because screen pixels are machine-specific.

Exit codes: 0 ok, 3 no desktop session / Tk, 4 capture did not match the window.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
PROTOTYPE = ROOT / "prototype"

WIDTH = 1200
HEIGHT = 200

#: Fixed subtitle script. Deliberately includes digits, punctuation, a wrapped
#: two-line card and mixed case, because those are what a subtitle strip carries.
SUBTITLES = (
    ["the sword intent of this sect is a myth"],
    ["gg wp noob"],
    ["chapter 216: the sentient and the leviathan"],
    ["why is the shadow so immortal?"],
    ["level 42 breakthrough unlocked!"],
    ["chapter 7: the aether gate reopened",
     "and the colony went dark for 3 days"],
    ["v1.30 signal lost... reconnecting"],
    ["the quantum engine is only a legend"],
    ["100% of the dark matter sample decayed"],
    ["chapter 9: the leviathan answered,",
     "but no one was listening"],
)


def dpi_aware() -> None:
    """Physical pixels for Tk geometry and mss, otherwise the window is scaled."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 - older Windows, or already aware
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def render(lines: list[str]) -> np.ndarray:
    frame = np.full((HEIGHT, WIDTH, 3), (18, 18, 18), dtype=np.uint8)
    step = 54
    total = len(lines) * step
    y = (HEIGHT - total) // 2 + 34
    for index, text in enumerate(lines):
        size, _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.25, 2)
        x = max(10, (WIDTH - size[0]) // 2)
        cv2.putText(frame, text, (x, y + index * step),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.25, (235, 235, 235), 2, cv2.LINE_AA)
    return frame


def write_clip(frames: list[np.ndarray], path: Path) -> None:
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (WIDTH, HEIGHT)
    )
    if not writer.isOpened():
        raise RuntimeError(f"cannot write video to {path}")
    for frame in frames:
        writer.write(frame)
    writer.release()


def decode_clip(path: Path) -> list[np.ndarray]:
    capture = cv2.VideoCapture(str(path))
    frames: list[np.ndarray] = []
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        frames.append(frame)
    capture.release()
    if not frames:
        raise RuntimeError(f"could not decode {path}")
    return frames


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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "w11_subtitles")
    parser.add_argument("--count", type=int, default=len(SUBTITLES))
    parser.add_argument("--position", default="60,60")
    parser.add_argument(
        "--title",
        default="",
        help="window title, so --capture window --window-title can find it",
    )
    parser.add_argument(
        "--hold",
        type=float,
        default=0.0,
        help="seconds to keep the window open after recording, so a live capture can read it",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="during --hold, replay the clip instead of freezing the last frame",
    )
    args = parser.parse_args(argv)

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    try:
        left, top = (int(part) for part in args.position.split(","))
    except ValueError:
        raise SystemExit("--position must look like 60,60")

    dpi_aware()
    try:
        import tkinter as tk

        from PIL import Image, ImageTk
        from mss import mss
    except ImportError as error:
        print(f"no desktop session: {error}", file=sys.stderr)
        return 3

    count = max(1, min(args.count, len(SUBTITLES)))
    sources = [render(SUBTITLES[index]) for index in range(count)]
    clip = out / "subtitles.mp4"
    write_clip(sources, clip)
    played = decode_clip(clip)[:count]

    try:
        root = tk.Tk()
        root.overrideredirect(True)  # decorations would offset the content area
        root.geometry(f"{WIDTH}x{HEIGHT}+{left}+{top}")
        root.attributes("-topmost", True)
        if args.title:
            root.title(args.title)
        label = tk.Label(root, borderwidth=0, highlightthickness=0)
        label.pack()
    except Exception as error:  # noqa: BLE001 - no display is a legitimate outcome
        print(f"no desktop session: {type(error).__name__}: {error}", file=sys.stderr)
        return 3

    engine = load_ocr()
    manifest = {
        "kind": "controlled-subtitles",
        "width": WIDTH,
        "height": HEIGHT,
        "count": count,
        "frames": [],
    }
    references = []

    try:
        with mss() as sct:
            region = {"left": left, "top": top, "width": WIDTH, "height": HEIGHT}
            for index, frame in enumerate(played):
                photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
                label.configure(image=photo)
                label.image = photo  # keep a reference or Tk drops it
                root.update()
                time.sleep(0.25)

                shot = np.array(sct.grab(region))
                captured = np.ascontiguousarray(shot[:, :, :3])
                if captured.shape[:2] != (HEIGHT, WIDTH):
                    raise RuntimeError(f"captured {captured.shape}, expected {WIDTH}x{HEIGHT}")

                # If the window is not where we think it is, this catches it.
                difference = float(np.abs(captured.astype(np.int16) - frame.astype(np.int16)).mean())
                if difference > 12.0:
                    print(
                        f"capture does not match the subtitle window "
                        f"(mean abs difference {difference:.1f}); refusing to record",
                        file=sys.stderr,
                    )
                    return 4

                name = f"subtitle_{index:02d}.bgr"
                np.ascontiguousarray(captured).tofile(out / name)
                manifest["frames"].append({"file": name})
                references.append({"frame": name, "lines": reference_for(engine, captured)})

        # A live --capture run needs the window to stay where it is for a while.
        # --loop replays the clip so the overlay sees changing text; without it
        # the last recorded frame stays on screen, which is the still case.
        if args.hold > 0:
            print(
                f"holding {WIDTH}x{HEIGHT}+{left}+{top} for {args.hold:.1f}s "
                f"({'looping' if args.loop else 'static'}); Ctrl+C to stop early",
                file=sys.stderr,
                flush=True,
            )
            photos = (
                [
                    ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
                    for frame in played
                ]
                if args.loop
                else []
            )
            deadline = time.monotonic() + args.hold
            tick = 0
            while time.monotonic() < deadline:
                if photos:
                    photo = photos[tick % len(photos)]
                    label.configure(image=photo)
                    label.image = photo
                    tick += 1
                root.update()
                time.sleep(0.25 if photos else 0.05)
    finally:
        root.destroy()

    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (out / "reference.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for record in references:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    detected = sum(1 for item in references if item["lines"])
    print(f"wrote {count} controlled subtitle strip(s) to {out} "
          f"({detected} with recognised text)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
