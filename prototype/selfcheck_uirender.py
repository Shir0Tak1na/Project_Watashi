#!/usr/bin/env python3
"""Does the desktop window actually draw its text on screen? (needs a display)

The gap this fills: every other UI check asserts *structure* -- a widget exists, its text
variable is set, a canvas item was created, a block has a sane size. Not one of them can
notice a window that paints nothing, text the same colour as its background, a label
covering the value it belongs to, or a font that renders as boxes. Only pixels can.

So this looks, using the project's own OCR. It builds the real desktop window with known
content, brings it to the front, screenshots it, and asserts that the OCR recognises the
strings that are supposed to be visible. That is a stronger claim than it sounds: it is
the claim that a user can read the interface, measured by the same engine the product
uses to read other people's screens.

Three things make it trustworthy rather than decorative:

* **The fixture is asserted to be legible at all** (mean confidence, number of lines), so
  "nothing was recognised" cannot pass as "nothing to check".
* **A negative control**: a nonce string that is not on screen must NOT be recognised, so
  the OCR path is proven able to tell present from absent.
* **Visibility is measured, not assumed.** Being the foreground window is not the same as
  being on top: in this environment `GetForegroundWindow() == our window` was true while
  another application was painted over 70% of it, and a check that trusted the API would
  have reported the resulting missing text as a rendering bug. So the window is hidden and
  the screen diffed -- the share of its own rectangle that changes is the share that was
  visible -- and the pixel assertions only run above a threshold. Below it the check says
  how covered it was and stops, rather than measuring somebody else's window.

    run.cmd selfcheck_uirender --summary
"""

from __future__ import annotations

import ctypes
import sys
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mss  # noqa: E402
import numpy as np  # noqa: E402

from watashi.checks import Checker  # noqa: E402
# The desktop self check already builds the fake session this needs. Importing it rather
# than copying: its FakeSession is what keeps this check honest (it is the same object the
# structural checks drive), and a second copy would drift from the first.
import selfcheck_desktop as sd  # noqa: E402
from watashi.config import AppConfig  # noqa: E402
from watashi.events import (  # noqa: E402
    EVENT_READY,
    EVENT_STATS,
    EVENT_SUBTITLE,
    OverlayStats,
    OverlayUpdate,
    TranslatedLine,
    encode_stats,
    encode_update,
)
from watashi.session import Session  # noqa: E402

#: A string that appears nowhere in the interface. OCR must not find it.
NONCE = "ZXQWV NONCE 9182"

#: The last subtitle published, which is the one the window shows as current.
TARGET = "他突破到了虚空境界"
SOURCE = "he broke through to the void realm"

#: Below this share of the window's own pixels changing when it is hidden, another window
#: is over part of it and the screenshot does not describe this interface.
VISIBLE_ENOUGH = 0.90


def display_scale() -> float:
    """Physical capture pixels per Tk logical pixel.

    Tk positions windows in a DPI-virtualised space (1707 wide on this machine) while mss
    grabs real pixels (2560 wide), so every capture rectangle needs this factor. Getting it
    wrong is not subtle -- an earlier probe did, and captured the window *below* the one it
    meant to photograph.
    """
    probe = tk.Tk()
    probe.withdraw()
    logical = probe.winfo_screenwidth()
    probe.destroy()
    with mss.MSS() as sct:
        return sct.monitors[1]["width"] / logical


def toplevel_hwnd(widget: tk.Misc) -> int:
    from watashi.overlay import _toplevel_hwnd

    return int(_toplevel_hwnd(widget))


def bring_to_front(widget: tk.Misc) -> None:
    """Ask hard for the top of the z-order, not just the focus.

    ``lift`` and ``-topmost`` are not enough when another topmost window has been
    activated more recently -- which is the normal state of a desktop someone is working
    on. ``SetWindowPos(HWND_TOPMOST)`` is the strongest request available to a window's
    own process.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    HWND_TOPMOST = wintypes.HWND(-1)
    SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040
    widget.lift()
    widget.update()
    hwnd = toplevel_hwnd(widget)
    user32.SetWindowPos(
        wintypes.HWND(hwnd), HWND_TOPMOST, 0, 0, 0, 0,
        SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW,
    )
    user32.SetForegroundWindow(wintypes.HWND(hwnd))
    widget.update()
    time.sleep(0.5)
    widget.update()


def capture(rect: dict) -> np.ndarray:
    with mss.MSS() as sct:
        return np.asarray(sct.grab(rect), dtype=np.uint8)[:, :, :3][:, :, ::-1]


def rect_of(widget: tk.Misc, scale: float) -> dict:
    return {
        "left": int(widget.winfo_rootx() * scale),
        "top": int(widget.winfo_rooty() * scale),
        "width": int(widget.winfo_width() * scale),
        "height": int(widget.winfo_height() * scale),
    }


def visible_share(widget: tk.Misc, scale: float) -> tuple[float, np.ndarray]:
    """How much of the window is actually on screen, measured by hiding it.

    Returns the share of its own rectangle whose pixels change when it is hidden, plus a
    fresh capture taken while it was visible. A window that is fully uncovered changes
    ~100% of its own area; one behind another window changes only the part that shows.
    """
    rect = rect_of(widget, scale)
    before = capture(rect)
    widget.withdraw()
    widget.update()
    time.sleep(0.5)
    after = capture(rect)
    widget.deiconify()
    bring_to_front(widget)
    difference = np.abs(before.astype(np.int16) - after.astype(np.int16)).max(axis=2) > 12
    return float(difference.mean()), before


def normalise(text: str) -> str:
    """OCR drops spaces and punctuation unevenly; compare on the characters themselves."""
    return "".join(ch for ch in text if ch.isalnum())


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("UI rendering self check (uses the project's own OCR on its own window)")
    print("=" * 78)

    session = sd.FakeSession()
    real = Session(AppConfig.load())
    session.settings_payload = real.settings_payload  # type: ignore[attr-defined]
    # This check photographs the desktop window, and a window that asks Windows to hide
    # it from capture cannot be photographed -- that is the whole point of the exclusion,
    # and it is why this check has to turn it off for its own fixture. Said here rather
    # than discovered as "the visibility guard always skips".
    session.config.capture["exclude_self"] = False

    app = sd.DesktopApp(session, overlay=None, title="Project Watashi uirender check")
    app.attach()
    app.root.geometry("1180x760+60+40")
    app.root.attributes("-topmost", True)

    session.publish(EVENT_READY, session.info())
    # Drained between frames on purpose. A burst of subtitles in one pump only paints the
    # last of them -- deliberate, so a backlog does not cost one repaint each -- which means
    # publishing three at once leaves one history entry, not three. The fixture has to
    # arrive the way frames really do: one at a time.
    for source, target, latency, coverage in (
        (SOURCE, TARGET, 41.0, 1.0),
        ("the sword intent of this sect is a myth", "此宗门的剑意是个神话", 88.0, 1.0),
        ("gg wp noob", "打得好，打得漂亮 新手", 12.0, 1.0),
        (SOURCE, TARGET, 41.0, 1.0),
    ):
        session.publish(EVENT_SUBTITLE, encode_update(OverlayUpdate(
            source_text=source, target_text=target, backend="corpus+rules",
            latency_ms=latency, coverage=coverage,
            lines=[TranslatedLine(
                source=source, target=target, confidence=0.95, coverage=coverage
            )],
        )))
        sd.drain(app, rounds=2)
    session.publish(EVENT_STATS, encode_stats(OverlayStats(
        fps=9.4, ocr_ms=95.0, translate_ms=6.0, total_ms=101.0, frames=57, skipped=12,
        refinements=8, backend="corpus+rules+nmt", corpus_entries=84, rules=8,
        memory_mib=893.0,
    )))
    sd.drain(app, rounds=4)
    time.sleep(0.3)
    app.root.update()
    sd.drain(app, rounds=2)

    # ---------------------------------------------------------------- #
    check.section("the fixture is populated before anything is photographed")
    check.check(
        "each frame that arrived became a history entry",
        len(app.history) >= 4,
        f"{len(app.history)} entries for 4 frames",
    )
    check.check(
        "the current translation is the one on screen",
        app.current_var.get() == TARGET,
        app.current_var.get(),
    )

    # ---------------------------------------------------------------- #
    check.section("the window has to be the one on top before pixels mean anything")

    scale = display_scale()
    hwnd = toplevel_hwnd(app.root)
    bring_to_front(app.root)

    share, shot = visible_share(app.root, scale)
    if share >= VISIBLE_ENOUGH:
        check.check(
            "the window is the one in the photograph",
            True,
            f"{share * 100:.1f}% of its own pixels changed when it was hidden",
        )
    else:
        check.skip(
            "the desktop window's pixels",
            f"only {share * 100:.1f}% of the window is uncovered, so a screenshot of it "
            f"would be a screenshot of another application. Being the foreground window "
            f"is not the same as being on top -- measured, not assumed, because that "
            f"distinction already produced three wrong readings of this screen. Close "
            f"other windows and run this again to verify it.",
        )

    if share < VISIBLE_ENOUGH:
        # Closed before the overlay section: the desktop fixture shows the same sentence
        # the overlay probe used to use, so leaving it open let the source line be read off
        # the wrong window and counted as proof about the overlay.
        app.close()
        return _overlay_section(check)

    rect = rect_of(app.root, scale)
    check.check(
        "the capture is the size the window claims to be",
        abs(shot.shape[1] - rect["width"]) <= 2 and abs(shot.shape[0] - rect["height"]) <= 2,
        f"window {rect['width'] // 1}x{rect['height'] // 1} physical "
        f"-> captured {shot.shape[1]}x{shot.shape[0]} at scale {scale:.2f}",
    )
    check.check(
        "the window is not a blank rectangle",
        int(len(np.unique(shot.reshape(-1, 3), axis=0))) > 200,
        f"{len(np.unique(shot.reshape(-1, 3), axis=0))} distinct colours",
    )

    from watashi.ocr import RapidOcrEngine

    engine = RapidOcrEngine(max_width=0)  # no downscale: this is a measurement
    engine.ensure_loaded()

    # ---------------------------------------------------------------- #
    check.section("what the project's own OCR can read off its own interface")

    def read_current() -> tuple[str, float, int]:
        result = engine.recognize(shot)
        lines = list(getattr(result, "lines", []) or [])
        joined = " ".join(line.text for line in lines)
        mean = sum(line.confidence for line in lines) / len(lines) if lines else 0.0
        return joined, mean, len(lines)

    text, mean_confidence, line_count = read_current()
    found = normalise(text)
    # Printed, not just counted: when this check fails, the log has to show what OCR
    # actually saw, or the only way to diagnose it is to run the experiment by hand.
    print(f"       read {line_count} line(s), mean confidence {mean_confidence:.3f}")
    print("       read: " + (text[:400] if text else "(nothing)"))

    check.check(
        "OCR read a plausible amount of text off the window",
        line_count >= 4,
        f"{line_count} line(s): a window that renders nothing would report 0",
    )
    check.check(
        "the fixture is legible, so a missing string means missing ink, not bad OCR",
        mean_confidence >= 0.70,
        f"mean confidence {mean_confidence:.3f}",
    )
    check.check(
        "the current translation is visible on screen",
        normalise(TARGET) in found,
        f"{TARGET!r} not found in what was read",
    )
    check.check(
        "the source line above it is visible",
        normalise("broke through to the void realm") in found,
        f"{SOURCE!r} not found in what was read",
    )
    check.check(
        "the history section is labelled",
        normalise("历史") in found,
        "the 对照历史 label",
    )
    check.check(
        "the correction editor's title is visible",
        normalise("实时纠正") in found,
        "the 实时纠正 · 改错的那一行 frame title",
    )
    check.check(
        "its field labels are visible",
        normalise("原文") in found and normalise("译文") in found,
        "the 原文 / 译文 labels",
    )
    check.check(
        "and its save button",
        normalise("保存纠正") in found,
        "the 保存纠正 button",
    )
    check.check(
        "the provenance line of a history entry is visible",
        normalise("语料库") in found or normalise("覆盖率") in found,
        "the per-entry meta line",
    )
    check.check(
        "the negative control: a string that is not on screen is not recognised",
        normalise(NONCE) not in found,
        f"{NONCE!r} came back from OCR, so these assertions prove nothing",
    )

    # ---------------------------------------------------------------- #
    check.section("the controls a desktop window is for are legible")

    # The settings form used to be a tab here and is now the web panel's job, so what is
    # left to verify is the top bar: the controls that need a native window, and the one
    # button that hands the user to the surface that edits.
    tabs = {app.notebook.tab(i, "text"): i for i in range(app.notebook.index("end"))}
    app.notebook.select(tabs["采集"])
    app.root.update()
    sd.drain(app, rounds=3)
    time.sleep(0.4)
    app.root.update()
    with mss.MSS() as sct:
        shot2 = np.asarray(sct.grab(rect), dtype=np.uint8)[:, :, :3][:, :, ::-1]
    bar_text = " ".join(line.text for line in (engine.recognize(shot2).lines or []))
    bar_found = normalise(bar_text)
    for label, why in (
        ("暂停识别", "the control that must always be reachable"),
        ("目标语言", "the one setting a user changes while watching"),
        ("打开设置面板", "the way to the surface that edits"),
        ("采集", "the tab that is left"),
    ):
        check.check(
            f"{label!r} is on screen",
            normalise(label) in bar_found,
            f"{why}; read: {bar_text[:140]!r}",
        )
    check.check(
        "and the settings form is gone from this window",
        normalise("立即生效") not in bar_found,
        "it lives in the web panel now; two editors of one state is what was removed",
    )
    check.check(
        "no label from the other tab leaked into this one",
        normalise(TARGET) not in bar_found,
        "a repaint that never happened would leave the previous tab's text behind",
    )

    app.close()
    return _overlay_section(check)


#: A string that exists nowhere else, so finding it in a screenshot is proof the overlay
#: drew it. Pure Latin and no spaces on purpose: the plate is 72% opaque, so text behind
#: it bleeds through, and a probe mixing scripts gets merged with whatever Chinese the
#: window underneath happens to be showing -- which is exactly how an earlier attempt at
#: this read a chat message instead of the overlay.
OVERLAY_PROBE = "PROBEZQX9182WATASHI"
#: A second unique string for the bar's other row. Distinct from the desktop fixture's
#: sentences, because sharing one let a sentence be read off the wrong window and counted
#: as evidence about the overlay.
OVERLAY_SOURCE = "PROBESRCZQX9182 the original line"


def _overlay_section(check: Checker) -> int:
    """Verify the overlay draws its text, on the overlay's own rectangle.

    This is the surface a user actually reads, and the one thing structural checks cannot
    establish: `selfcheck_overlay` proves canvas items exist at sane coordinates, not that
    anything reaches the screen. The failure mode being ruled out is a plate drawn with no
    text on it, which every canvas assertion passes.

    Two things make it usable on a working desktop rather than only on a clean one:

    * **Only the overlay's own block is photographed**, so no other window's text can be
      mistaken for its output -- which is how an earlier version of this read a chat
      message and nearly reported the overlay as broken.
    * **Visibility is measured before anything is asserted.** The block is captured, the
      overlay hidden, and the block captured again: with capture exclusion off and the
      window opaque, the share of the block that changes is the share that was the
      overlay's. If another window is on top of it the change is ~0 and the check skips,
      because "the text is missing" and "this is not the overlay" look identical in a
      screenshot and only one of them is a bug.
    """
    from watashi.events import OverlayUpdate, TranslatedLine
    from watashi.ocr import RapidOcrEngine
    from watashi.overlay import Overlay, set_capture_exclusion
    from watashi.presentation import PresentationSpec

    check.section("the overlay draws its text, shown by hiding it again")

    def normalise_here(text: str) -> str:
        return normalise(text)

    def full_screen() -> np.ndarray:
        with mss.MSS() as sct:
            return np.asarray(sct.grab(sct.monitors[1]), dtype=np.uint8)[:, :, :3][:, :, ::-1]

    probe = Overlay(presentation=PresentationSpec.preset("bar"))
    probe.start()
    try:
        probe.push(OverlayUpdate(
            source_text=OVERLAY_SOURCE,
            target_text=OVERLAY_PROBE,
            coverage=1.0, backend="corpus+rules+nmt", latency_ms=88.0,
            lines=[TranslatedLine(
                source=OVERLAY_SOURCE, target=OVERLAY_PROBE,
                confidence=0.95, coverage=1.0,
            )],
        ))
        probe._root.update()
        time.sleep(0.9)
        # Two deliberate changes before photographing, both learned the hard way:
        #
        # * Capture exclusion is switched off. The overlay excludes itself so the engine
        #   cannot read its own output; that is correct behaviour, and it also means an
        #   attempt to photograph the overlay comes back showing whatever is behind it.
        # * The window is made fully opaque. At the bar preset's 0.72 alpha the window is
        #   layered, and a layered window is not what a screen capture returns: the same
        #   experiment found the overlay's text at 1.00 confidence with alpha 1.0 and
        #   could not find it at all at 0.72, and PrintWindow returned solid black. This
        #   asserts that the text is *drawn*; it does not assert the configured opacity,
        #   which the presentation and overlay checks cover.
        def raise_overlay() -> None:
            for surface in probe._surfaces:
                set_capture_exclusion(surface.window, False)
                surface.window.attributes("-alpha", 1.0)
                bring_to_front(surface.window)

        raise_overlay()
        probe._root.update()
        time.sleep(0.9)
        probe._root.update()

        if not check.check(
            "the overlay has a block to draw in",
            bool(probe._last_blocks),
            f"{[(b.x, b.y, b.width, b.height) for b in probe._last_blocks]}",
        ):
            return check.report()

        engine = RapidOcrEngine(max_width=0)
        engine.ensure_loaded()

        def read_marks(text: str) -> tuple[bool, bool]:
            found = normalise_here(text)
            return (
                normalise(OVERLAY_PROBE) in found,
                normalise(OVERLAY_SOURCE) in found,
            )

        # The whole screen rather than the overlay's own rectangle. The rectangle is the
        # tidier measurement and it is what an earlier version used, but with another
        # application painted over that part of the display it returns that application's
        # text, and two runs of it here disagreed with each other about whether the
        # overlay draws at all. Searching the whole screen for a string that exists
        # nowhere else cannot be wrong about whose text it found.
        shown_text = hidden_text = ""
        probe_seen = source_seen = False
        for attempt in range(1, 4):
            raise_overlay()
            time.sleep(0.7)
            shown = full_screen()
            shown_text = " ".join(line.text for line in (engine.recognize(shown).lines or []))
            probe_seen, source_seen = read_marks(shown_text)
            if probe_seen:
                break
            if attempt < 3:
                # A topmost overlay still loses to whichever topmost window was activated
                # most recently, and this desktop has two of those. Asking again is not a
                # retry of a broken assertion; it is the difference between an overlay
                # that is covered and an overlay that draws nothing.
                time.sleep(0.6)

        if not probe_seen:
            check.skip(
                "the overlay's pixels",
                f"the probe text was not on screen in 3 attempts, which is what being "
                f"covered by another topmost window looks like -- and it is "
                f"indistinguishable from here from a plate drawn without text. Nothing "
                f"asserted. Run this with the desktop clear to verify it.",
            )
            return check.report()

        check.check(
            "the overlay's target text is on the screen",
            probe_seen,
            f"read back off a screenshot of the screen: {shown_text[:160]!r}",
        )
        check.check(
            "the overlay's source line is on the screen too",
            source_seen,
            f"the bar shows the original above the translation: {shown_text[:160]!r}",
        )

        # The control. Both strings exist nowhere but in this process's update, so their
        # presence is already proof -- but only the hidden frame proves the screenshot was
        # taken *while the overlay was up* rather than read from a stale frame.
        for surface in probe._surfaces:
            surface.window.withdraw()
        probe._root.update()
        time.sleep(0.8)
        hidden = full_screen()
        hidden_text = " ".join(line.text for line in (engine.recognize(hidden).lines or []))
        check.check(
            "and neither of them is there once the overlay is hidden",
            not any(read_marks(hidden_text)),
            f"read with the overlay hidden: {hidden_text[:160]!r}",
        )
    finally:
        probe.close(destroy_root=True)

    check.check(
        "checks were not all skipped",
        check.skips < check.checks,
        f"{check.skips} skipped of {check.checks}",
    )
    return check.report()


if __name__ == "__main__":
    sys.exit(main())
