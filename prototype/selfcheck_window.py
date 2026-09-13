#!/usr/bin/env python3
"""Window selection verification. Needs a display.

Covers the claim that matters most about capturing a *window* rather than a
dragged rectangle: **the capture follows the window**. A region is a snapshot of
one moment -- move the video player and it silently reads whatever now occupies
the old coordinates. This asserts that the resolved rectangle actually tracks a
window that moves, and that the captured pixels move with it.

It also asserts the negative: after moving, the *old* location must no longer
show the window's content. Without that half, a stale frame would look like a
pass.

    python selfcheck_window.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

from watashi.checks import Checker
from watashi.winutil import list_windows, resolve_window

MARKER = "#ff00ff"  # a colour no normal desktop produces
MARKER_BGR = np.array((255, 0, 255), dtype=np.int16)
TITLE = "watashi-window-probe"
POS_A = (200, 200, 420, 300)
POS_B = (700, 520, 420, 300)


def share_of(frame: np.ndarray, colour: np.ndarray, tol: int = 40) -> float:
    return float(np.all(np.abs(frame.astype(np.int16) - colour) < tol, axis=2).mean())


def main() -> int:
    import tkinter as tk

    check = Checker()
    print("=" * 78)
    print("Window selection self check (requires a display)")
    print("=" * 78)

    root = tk.Tk()
    root.withdraw()

    window = tk.Toplevel(root)
    window.title(TITLE)
    window.overrideredirect(True)
    window.attributes("-topmost", True)
    window.geometry(f"{POS_A[2]}x{POS_A[3]}+{POS_A[0]}+{POS_A[1]}")
    tk.Frame(window, bg=MARKER).pack(fill="both", expand=True)
    window.update_idletasks()
    window.update()
    time.sleep(0.5)
    window.update()

    try:
        print("")
        print("-- enumeration --")
        windows = list_windows()
        check.check("windows are enumerated", len(windows) > 0, f"{len(windows)} candidates")
        found = [w for w in windows if w.title == TITLE]
        check.check("the probe window is listed", len(found) == 1, f"{len(found)} match(es)")
        check.check("it reports a client area",
                    found and found[0].rect[2] > 100 and found[0].rect[3] > 100,
                    found[0].size_label if found else "-")

        print("")
        print("-- resolution and error reporting --")
        by_title, err = resolve_window(TITLE)
        check.check("resolved by exact title", by_title is not None and not err, err or "ok")
        by_partial, err2 = resolve_window("window-probe")
        check.check("resolved by title substring", by_partial is not None and not err2, err2 or "ok")
        index = next((i for i, w in enumerate(windows) if w.title == TITLE), None)
        if index is not None:
            by_index, err3 = resolve_window(str(index))
            check.check("resolved by list index",
                        by_index is not None and by_index.title == TITLE, err3 or "ok")
        _, bad_index = resolve_window("9999")
        check.check("an out-of-range index explains the range",
                    "out of range" in bad_index, bad_index)
        _, no_match = resolve_window("zzz-definitely-not-a-window")
        check.check("a missing title says so", "no window title contains" in no_match, no_match)

        print("")
        print("-- capture follows the window --")
        from watashi.capture import WindowCapturer

        capturer = WindowCapturer(spec=TITLE)
        rect_a = capturer.region
        check.check("initial client rect matches where the window was placed",
                    rect_a is not None
                    and abs(rect_a.x - POS_A[0]) <= 4 and abs(rect_a.y - POS_A[1]) <= 4,
                    f"{rect_a} vs requested ({POS_A[0]},{POS_A[1]})")

        frame_a = capturer.grab()
        share_a = share_of(frame_a, MARKER_BGR)
        check.check("the window's content is captured", share_a > 0.5,
                    f"marker={share_a * 100:.0f}% of {frame_a.shape[1]}x{frame_a.shape[0]}")

        # ---- move it, without touching the capturer at all ---------------- #
        window.geometry(f"{POS_B[2]}x{POS_B[3]}+{POS_B[0]}+{POS_B[1]}")
        window.update_idletasks()
        window.update()
        time.sleep(0.6)
        window.update()

        rect_b = capturer.region
        check.check(
            "the reported rectangle moved WITHOUT re-resolving the window",
            rect_b is not None
            and abs(rect_b.x - POS_B[0]) <= 4 and abs(rect_b.y - POS_B[1]) <= 4,
            f"{rect_b} vs new ({POS_B[0]},{POS_B[1]})",
        )
        check.check("the rectangle actually changed", rect_a != rect_b,
                    "a fixed region would be identical")

        frame_b = capturer.grab()
        share_b = share_of(frame_b, MARKER_BGR)
        check.check("the content is still captured after the move", share_b > 0.5,
                    f"marker={share_b * 100:.0f}%")

        # negative control: the old location must NOT still show the window
        import mss

        with mss.MSS() as sct:
            old = np.asarray(
                sct.grab({"left": POS_A[0], "top": POS_A[1],
                          "width": POS_A[2], "height": POS_A[3]}),
                dtype=np.uint8,
            )[:, :, :3]
        old_share = share_of(old, MARKER_BGR)
        check.check(
            "the OLD location no longer shows it (so this is not a stale frame)",
            old_share < 0.05,
            f"marker={old_share * 100:.0f}% at the old position",
        )

        print("")
        print("-- sub-region: capture only part of the window --")
        from watashi.capture import WindowCapturer as WC

        bottom = WC(spec=TITLE, sub_region=(0.0, 0.5, 1.0, 1.0))
        rect_sub = bottom.region
        full_rect = capturer.region
        check.check(
            "a bottom-half sub-region halves the height and shifts the origin",
            rect_sub is not None
            and full_rect is not None
            and abs(rect_sub.height - full_rect.height // 2) <= 2
            and abs(rect_sub.y - (full_rect.y + full_rect.height // 2)) <= 2,
            f"sub={rect_sub} full={full_rect}",
        )
        check.check(
            "the sub-region is still inside the window",
            rect_sub is not None
            and full_rect is not None
            and rect_sub.y >= full_rect.y
            and rect_sub.y + rect_sub.height <= full_rect.y + full_rect.height + 2,
        )
        for spec in ("0,0.5,1,1", "0.1,0.2,0.9,0.8"):
            ok = True
            try:
                WC.parse_sub_region(spec)
            except ValueError:
                ok = False
            check.check(f"sub-region spec {spec!r} parses", ok)
        for spec in ("0,0.5,1", "0,0.9,1,0.1", "-1,0,1,1"):
            rejected = False
            try:
                WC.parse_sub_region(spec)
            except ValueError:
                rejected = True
            check.check(f"invalid sub-region {spec!r} is rejected", rejected)

        print("")
        print("-- our own windows are never offered as targets --")
        import os

        others = list_windows(exclude_pid=os.getpid())
        check.check("exclude_pid removes this process's windows",
                    all(w.pid != os.getpid() for w in others),
                    f"{len(others)} window(s) from other processes")

    finally:
        window.destroy()
        root.destroy()

    return check.report()


if __name__ == "__main__":
    raise SystemExit(main())
