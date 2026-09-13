#!/usr/bin/env python3
"""Drag-to-select verification. Needs a display, but no human.

The selector's event handlers are methods, so a synthetic drag via
``event_generate(rootx=..., rooty=...)`` drives the whole path. That matters:
the coordinate bug this file exists to catch survived precisely because the only
way to notice it was to drag by hand and look at where the box ended up.

What it asserts:

* a drag yields exactly the region the user described, **in physical pixels**
* on a scaled display that is *not* the logical box, so the conversion is doing
  real work rather than being a no-op that trivially passes
* a stray click is a cancel, not a 1x1 region
* Esc cancels

    python selfcheck_selector.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker
from watashi.capture import display_scale, list_monitors, logical_to_physical


def drag(selector, from_xy: tuple[int, int], to_xy: tuple[int, int]) -> None:
    """Synthesize a press-drag-release at absolute (root) coordinates."""
    canvas = selector._canvas
    canvas.event_generate("<ButtonPress-1>", x=5, y=5, rootx=from_xy[0], rooty=from_xy[1])
    canvas.event_generate("<B1-Motion>", x=to_xy[0] % 100, y=to_xy[1] % 100,
                          rootx=to_xy[0], rooty=to_xy[1])
    canvas.event_generate("<ButtonRelease-1>", x=to_xy[0] % 100, y=to_xy[1] % 100,
                          rootx=to_xy[0], rooty=to_xy[1])
    canvas.update()


def main() -> int:
    import tkinter as tk

    check = Checker()
    print("=" * 78)
    print("Drag-to-select self check (needs a display, no human)")
    print("=" * 78)

    monitor = list_monitors()[1]
    physical = (int(monitor["width"]), int(monitor["height"]))

    root = tk.Tk()
    root.withdraw()
    logical = (root.winfo_screenwidth(), root.winfo_screenheight())
    scale = display_scale(logical, physical)

    print("")
    print(f"  Tk logical screen : {logical[0]}x{logical[1]}")
    print(f"  physical screen   : {physical[0]}x{physical[1]}")
    print(f"  scale             : {scale:.4f}")

    from watashi.selector import RegionSelector

    # ---- a real drag ---------------------------------------------------- #
    print("")
    print("-- a synthetic drag through the real handlers --")
    selector = RegionSelector(physical_screen=physical, root=root)
    selector.build()
    check.check("the selector reports the display scale",
                abs(selector.scale - scale) < 0.01,
                f"selector={selector.scale:.4f} direct={scale:.4f}")

    start, end = (300, 250), (900, 700)
    drag(selector, start, end)

    logical_box = (start[0], start[1], end[0] - start[0], end[1] - start[1])
    expected = logical_to_physical(logical_box, scale)

    check.check("the drag produced a region", selector.result is not None)
    if selector.result is not None:
        got = (selector.result.x, selector.result.y, selector.result.width, selector.result.height)
        check.check(
            "the region matches the drag, converted to physical pixels",
            got == expected,
            f"got={got} expected={expected}",
        )
        check.check(
            "the logical box was recorded for display/debugging",
            selector.logical_box == logical_box,
            f"{selector.logical_box}",
        )
        if scale != 1.0:
            check.check(
                "the conversion is doing real work (physical != logical)",
                got != logical_box,
                f"physical={got} logical={logical_box}",
            )
            check.check(
                "the old behaviour would have been wrong here",
                logical_box != expected,
                f"a straight pass-through would capture {logical_box}, "
                f"the user dragged {expected}",
            )

    # ---- a backwards drag, to prove min/max handling -------------------- #
    print("")
    print("-- dragging up-and-left --")
    selector2 = RegionSelector(physical_screen=physical, root=root)
    selector2.build()
    drag(selector2, (900, 700), (300, 250))
    backwards_box = (300, 250, 600, 450)
    expected2 = logical_to_physical(backwards_box, scale)
    check.check(
        "a backwards drag yields the same rectangle, not a negative one",
        selector2.result is not None
        and (selector2.result.x, selector2.result.y,
             selector2.result.width, selector2.result.height) == expected2,
        f"{selector2.result} vs {expected2}",
    )

    # ---- a stray click is a cancel -------------------------------------- #
    print("")
    print("-- a click without a drag --")
    selector3 = RegionSelector(physical_screen=physical, root=root)
    selector3.build()
    drag(selector3, (500, 500), (505, 503))
    check.check(
        "a few-pixel click cancels instead of capturing a useless region",
        selector3.result is None,
        f"result={selector3.result}",
    )

    # ---- Escape cancels -------------------------------------------------- #
    print("")
    print("-- Escape --")
    selector4 = RegionSelector(physical_screen=physical, root=root)
    selector4.build()
    selector4.on_press(type("E", (), {"x_root": 100, "y_root": 100, "x": 1, "y": 1})())
    selector4.on_cancel()
    check.check("Escape cancels", selector4.result is None)

    # ---- the conversion helpers themselves ------------------------------ #
    print("")
    print("-- conversion helpers --")
    check.check("a 1.0 scale is a pass-through",
                logical_to_physical((10, 20, 30, 40), 1.0) == (10, 20, 30, 40))
    check.check("a 1.5 scale scales position and size",
                logical_to_physical((100, 200, 300, 400), 1.5) == (150, 300, 450, 600))
    check.check("a size never collapses to zero",
                logical_to_physical((0, 0, 0, 0), 0.01)[2:] == (1, 1))
    check.check("unknown physical size degrades to 1.0 rather than dividing by zero",
                display_scale((1920, 1080), None) == 1.0)
    check.check("rounding noise near 1.0 is treated as 1.0",
                display_scale((1000, 500), (1005, 502)) == 1.0)

    # ---- force a non-1.0 scale ------------------------------------------ #
    # Tk's reported screen size depends on whether the process is DPI-aware, and
    # mss makes it aware -- so on this machine the scale is often exactly 1.0 and
    # the conversion above degenerates to a pass-through. That would leave the
    # interesting path untested, so the scale is forced to a non-trivial value:
    # the handlers read self.scale, so this drives the real code.
    print("")
    print("-- the conversion path, with the scale forced to 1.5 --")
    forced = RegionSelector(physical_screen=physical, root=root)
    forced.build()
    forced.scale = 1.5
    drag(forced, (400, 400), (1200, 700))
    logical_box = (400, 400, 800, 300)
    expected = logical_to_physical(logical_box, 1.5)
    check.check(
        "a drag under a 1.5 scale is converted, not passed through",
        forced.result is not None
        and (forced.result.x, forced.result.y,
             forced.result.width, forced.result.height) == expected,
        f"got={forced.result} expected={expected} "
        f"(pass-through would be {logical_box})",
    )
    check.check(
        "and the result differs from the raw logical box",
        forced.result is not None and (
            forced.result.x, forced.result.y, forced.result.width, forced.result.height
        ) != logical_box,
        "this is the bug the old selector had",
    )

    # ---- re-selecting while the overlay is running ----------------------- #
    # Driven through the overlay's own queue: the hotkey fires on another thread
    # and must not touch Tk, so this is the path that actually runs in the app.
    print("")
    print("-- re-selecting at runtime, through the overlay's queue --")
    from watashi.events import OverlayUpdate
    from watashi.overlay import Overlay
    from watashi.presentation import PresentationSpec

    overlay = Overlay(
        presentation=PresentationSpec.preset("minimal"),
        screen_size=logical,
        physical_screen=physical,
        region_box=(0, 0, 400, 200),
    )
    overlay.start()
    overlay.push(OverlayUpdate(source_text="probe", target_text="PROBE"))
    applied: list[tuple[int, int, int, int]] = []
    overlay.on_reselect = lambda region: applied.append(
        (region.x, region.y, region.width, region.height)
    )
    for _ in range(20):
        overlay._root.update()
        time.sleep(0.02)

    overlay.request_reselect()
    selector = None
    for _ in range(120):
        overlay._root.update()
        selector = overlay._active_selector
        if selector is not None:
            break
        time.sleep(0.02)
    check.check("the hotkey path opens a selector", selector is not None)
    check.check("the overlay hid itself while selecting",
                selector is not None and all(s._hidden for s in overlay._surfaces))

    if selector is not None:
        drag(selector, (200, 150), (1000, 620))
        for _ in range(60):
            overlay._root.update()
            if applied:
                break
            time.sleep(0.02)
        want = logical_to_physical((200, 150, 800, 470), selector.scale)
        check.check(
            "the dragged region reaches the engine callback, in physical pixels",
            applied and applied[0] == want,
            f"applied={applied} expected={want}",
        )
        check.check("the selector closed itself afterwards",
                    overlay._active_selector is None)
        check.check("the overlay became visible again",
                    overlay.visible and any(not s._hidden for s in overlay._surfaces))

    # cancelling must not apply anything
    applied.clear()
    overlay.request_reselect()
    selector2 = None
    for _ in range(120):
        overlay._root.update()
        selector2 = overlay._active_selector
        if selector2 is not None:
            break
        time.sleep(0.02)
    if selector2 is not None:
        selector2.on_cancel()
        for _ in range(40):
            overlay._root.update()
            time.sleep(0.01)
        check.check("cancelling applies no region", not applied, f"applied={applied}")
    overlay.close()

    root.destroy()
    return check.report()


if __name__ == "__main__":
    raise SystemExit(main())
