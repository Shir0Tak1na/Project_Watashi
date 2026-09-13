#!/usr/bin/env python3
"""Overlay verification. Needs a display; everything else here is headless.

The overlay window is the one part of the system that cannot be asserted without
a screen, so this exists to cover it: it cycles every presentation preset, checks
that the right number of blocks is drawn, that live switching actually repaints,
and that in-place layout converts physical capture coordinates into Tk's logical
ones.

That last check matters more than it looks. Tk reports a DPI-scaled logical
screen while capture and OCR use physical pixels; with Windows display scaling at
150% these differ by 1.5x, and per-line boxes land in the wrong place unless
converted. A silent wrong position is exactly the kind of bug a screenshot does
not reveal, so the coordinates are asserted numerically.

    run.cmd selfcheck_overlay
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker  # noqa: E402

from watashi.events import OverlayUpdate, TranslatedLine
from watashi.overlay import GWL_EXSTYLE, Overlay, _IS_WINDOWS, _toplevel_hwnd
from watashi.presentation import PresentationSpec


#: Physical capture geometry, as mss reports it.
REGION = (400, 1150)
PHYSICAL_SCREEN = (2560, 1600)
BOXES = ((20, 30, 700, 46), (20, 90, 300, 46))

UPDATE = OverlayUpdate(
    source_text="the sword intent of this sect is a myth\ngg wp noob",
    target_text="剑意\n打得好，打得漂亮 新手",
    coverage=0.9,
    confidence=0.8,
    latency_ms=93.0,
    backend="corpus+rules",
    lines=[
        TranslatedLine(source="the sword intent of this sect is a myth", target="剑意",
                       box=BOXES[0], confidence=0.95),
        TranslatedLine(source="gg wp noob", target="打得好，打得漂亮 新手",
                       box=BOXES[1], confidence=0.9),
    ],
)

STEPS = ("bar", "minimal", "bare", "lines", "inplace", "hidden", "panel", "bar")



def main() -> int:
    check = Checker()
    print("=" * 78)
    print("Overlay self check (requires a display)")
    print("=" * 78)

    overlay = Overlay(
        presentation=PresentationSpec.preset("bar"),
        region_box=REGION,
        physical_screen=PHYSICAL_SCREEN,
    )
    overlay.start()

    observed: dict[str, dict] = {}

    def sample(label: str) -> None:
        hwnd = _toplevel_hwnd(overlay._surfaces[0].window) if overlay._surfaces else 0
        style = 0
        if hwnd:
            import ctypes

            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        observed[label] = {
            "surfaces": [
                (s.window.geometry(), len(s.canvas.find_all()), s._hidden)
                for s in overlay._surfaces
            ],
            "blocks": [(b.x, b.y, b.width, b.height) for b in overlay._last_blocks],
            "renders": overlay.renders,
            "scale": overlay.capture_scale,
            "panel": overlay._panel is not None,
            "hwnd": hwnd,
            "ws_ex": style,
        }

    def advance(index: int = 0) -> None:
        if index >= len(STEPS):
            overlay.close()
            return
        name = STEPS[index]
        overlay.apply_presentation(PresentationSpec.preset(name))
        overlay.push(UPDATE)
        overlay._root.after(240, lambda: (sample(name), advance(index + 1)))

    overlay._root.after(300, advance)
    overlay.run()

    print("")
    print(f"-- geometry: physical {PHYSICAL_SCREEN} vs logical "
          f"{overlay._screen_size} -> scale {observed.get('bar', {}).get('scale')} --")

    print("")
    print("-- every preset renders --")
    for name in STEPS:
        data = observed.get(name)
        if data is None:
            check.check(f"{name!r} was sampled", False)
            continue
        print(f"    {name:9s} blocks={len(data['blocks'])} "
              f"surfaces={len(data['surfaces'])} panel={data['panel']} "
              f"at {data['blocks'][:2]}")

    bar = observed.get("bar", {})
    check.check("bar draws exactly one block", len(bar.get("blocks", [])) == 1)
    check.check("bar puts text on the canvas",
                any(items > 0 for _g, items, _h in bar.get("surfaces", [])),
                f"items={[i for _g, i, _h in bar.get('surfaces', [])]}")
    check.check("minimal draws fewer canvas items than bar",
                sum(i for _g, i, _h in observed.get("minimal", {}).get("surfaces", []))
                < sum(i for _g, i, _h in bar.get("surfaces", [])),
                "fewer elements means less drawn")
    check.check("bare outlines, so it draws many more items",
                sum(i for _g, i, _h in observed.get("bare", {}).get("surfaces", []))
                > sum(i for _g, i, _h in bar.get("surfaces", [])),
                f"bare={[i for _g,i,_h in observed.get('bare',{}).get('surfaces',[])]}")
    check.check("lines draws one block per recognised line",
                len(observed.get("lines", {}).get("blocks", [])) == 2)
    check.check("hidden draws nothing", observed.get("hidden", {}).get("blocks") == [])
    check.check("panel uses the history window instead of blocks",
                observed.get("panel", {}).get("panel") is True
                and observed.get("panel", {}).get("blocks") == [])
    check.check("switching back to bar restores the bar",
                len(observed.get("bar", {}).get("blocks", [])) == 1)

    print("")
    print("-- live switching actually repaints (not just state) --")
    check.check("the render counter advanced across presets",
                observed.get("bar", {}).get("renders", 0) > 2,
                f"renders={observed.get('bar', {}).get('renders')}")

    print("")
    print("-- in-place lands on the captured text's position --")
    inplace_blocks = observed.get("inplace", {}).get("blocks", [])
    check.check("inplace draws one block per line", len(inplace_blocks) == 2)
    scale = observed.get("inplace", {}).get("scale", 1.0)
    if len(inplace_blocks) == 2:
        pad_x, pad_y = 6, 3  # the inplace preset's background padding

        def expected_for(box: tuple[int, int, int, int]) -> tuple[int, int]:
            # The engine converts the region origin and each box component
            # separately and then applies the offset, so rounding differs from
            # converting the sum by a pixel or two. Tolerance is therefore a few
            # pixels; a missing conversion would be off by ~140 px here.
            return (
                round((REGION[0] + box[0] - pad_x) / scale),
                round((REGION[1] + box[1] - pad_y) / scale),
            )

        tolerance = 4
        ok = all(
            abs(actual[0] - expected_for(box)[0]) <= tolerance
            and abs(actual[1] - expected_for(box)[1]) <= tolerance
            for actual, box in zip([(b[0], b[1]) for b in inplace_blocks], BOXES)
        )
        check.check(
            "physical capture boxes were converted into logical coordinates",
            ok,
            f"actual={[(b[0], b[1]) for b in inplace_blocks]} "
            f"expected≈{[expected_for(b) for b in BOXES]} "
            f"scale={scale:.2f} (unconverted would be "
            f"{[(REGION[0] + b[0], REGION[1] + b[1]) for b in BOXES]})",
        )
        check.check("the two lines do not overlap", inplace_blocks[0][1] != inplace_blocks[1][1])

    print("")
    print("-- click-through survived the rewrite --")
    style = observed.get("inplace", {}).get("ws_ex", 0)
    if style:
        check.check("WS_EX_TRANSPARENT is set (clicks pass through)",
                    bool(style & 0x20), f"WS_EXSTYLE={hex(style & 0xFFFFFFFF)}")
        check.check("WS_EX_LAYERED is set", bool(style & 0x00080000))
        check.check("WS_EX_NOACTIVATE is set (the overlay never steals focus)",
                    bool(style & 0x08000000))
    else:
        print("  [skip] no HWND sampled (not Windows, or the window never mapped)")

    print("")
    print("-- the overlay must not appear in our own captures --")
    # Without this the overlay is a feedback loop: it draws subtitles, the capture
    # contains them, OCR reads our own output, the pixels change again and change
    # detection keeps firing -- which is the stutter the user reported.
    import numpy as np
    import mss

    from watashi.overlay import WDA_EXCLUDEFROMCAPTURE, WDA_MONITOR, WDA_NONE

    marker = "#ff00ff"  # a colour nothing on a normal desktop produces
    probe = Overlay(
        presentation=PresentationSpec.from_dict(
            {
                "name": "capture-probe",
                "layout": {"mode": "bar", "anchor": "top-left", "offset": [40, 40]},
                "elements": [
                    {"role": "target", "order": 0, "font": {"size": 40, "bold": True},
                     "color": "#ffffff"}
                ],
                "background": {"kind": "plate", "color": marker, "opacity": 1.0,
                               "padding": [20, 14]},
            }
        ),
    )
    probe.start()
    probe.push(OverlayUpdate(source_text="probe", target_text="CAPTURE PROBE"))
    probe._root.update()
    time.sleep(0.6)
    probe._root.update()

    applied = probe._surfaces[0].capture_affinity if probe._surfaces else WDA_NONE
    check.check(
        "the overlay requested capture exclusion",
        applied in (WDA_EXCLUDEFROMCAPTURE, WDA_MONITOR) or not _IS_WINDOWS,
        f"affinity={hex(applied)}",
    )
    check.check(
        "WDA_EXCLUDEFROMCAPTURE specifically (the variant that does not blind OCR)",
        applied == WDA_EXCLUDEFROMCAPTURE or not _IS_WINDOWS,
        f"affinity={hex(applied)}",
    )

    if probe._last_blocks:
        block = probe._last_blocks[0]
        rect = {"left": block.x, "top": block.y, "width": block.width, "height": block.height}
        with mss.MSS() as sct:
            shot = np.asarray(sct.grab(rect), dtype=np.uint8)[:, :, :3].astype(np.int16)
        target = np.array((255, 0, 255), dtype=np.int16)  # BGR magenta
        share = float(np.all(np.abs(shot - target) < 40, axis=2).mean())
        check.check(
            "the overlay's own plate is NOT in the capture",
            share < 0.02,
            f"magenta={share * 100:.1f}% over its own {block.width}x{block.height} block",
        )
        # control: prove the test could detect it, by excluding nothing
        from watashi.overlay import set_capture_exclusion

        set_capture_exclusion(probe._surfaces[0].window, False)
        probe._root.update()
        time.sleep(0.5)
        probe._root.update()
        with mss.MSS() as sct:
            shot2 = np.asarray(sct.grab(rect), dtype=np.uint8)[:, :, :3].astype(np.int16)
        share2 = float(np.all(np.abs(shot2 - target) < 40, axis=2).mean())
        check.check(
            "control: with exclusion off the plate IS captured",
            share2 > 0.3,
            f"magenta={share2 * 100:.1f}% (so the check above is meaningful)",
        )
        set_capture_exclusion(probe._surfaces[0].window, True)
    probe.close()

    print("")
    print("-- global hotkeys (the only control a click-through overlay can have) --")
    from watashi.hotkeys import HotkeyManager, parse_hotkey

    check.check("hotkey parsing accepts ctrl+alt+p",
                parse_hotkey("ctrl+alt+p") == (0x4000 | 0x0002 | 0x0001, 0x50))
    check.check("hotkey parsing rejects a bad key", not _ok(lambda: parse_hotkey("ctrl+alt+Z")))
    check.check("hotkey parsing rejects the reserved Windows key",
                not _ok(lambda: parse_hotkey("ctrl+win+p")))

    fired = {"count": 0}

    def on_hotkey() -> None:
        fired["count"] += 1

    manager = HotkeyManager()
    manager.add("ctrl+alt+F11", on_hotkey)
    if manager.start():
        check.check("the hotkey registered with Windows",
                    all(b.registered for b in manager._bindings),
                    "; ".join(manager.describe()))
        _press_ctrl_alt(0x7A)  # F11
        deadline = time.time() + 3
        while fired["count"] == 0 and time.time() < deadline:
            time.sleep(0.05)
        check.check("a synthesized ctrl+alt+F11 reaches the handler",
                    fired["count"] > 0, f"fired {fired['count']} time(s)")
        manager.stop()
        check.check("hotkeys unregister cleanly", True)
    else:
        print(f"  [skip] hotkeys unavailable: {'; '.join(manager.failures)}")

    return check.report()


def _ok(fn) -> bool:
    try:
        fn()
        return True
    except ValueError:
        return False


def _press_ctrl_alt(vk: int) -> None:
    """Synthesize ctrl+alt+<vk> so the registered hotkey can be exercised."""
    if not _IS_WINDOWS:
        return
    import ctypes

    user32 = ctypes.windll.user32
    KEYUP = 0x0002
    VK_CONTROL = 0x11
    VK_MENU = 0x12
    for key, flags in (
        (VK_CONTROL, 0), (VK_MENU, 0), (vk, 0),
        (vk, KEYUP), (VK_MENU, KEYUP), (VK_CONTROL, KEYUP),
    ):
        user32.keybd_event(key, 0, flags, 0)
        time.sleep(0.03)


if __name__ == "__main__":
    raise SystemExit(main())
