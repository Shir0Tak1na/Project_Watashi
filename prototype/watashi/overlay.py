"""On-screen overlay: a spec-driven painter for the floating subtitle window.

The overlay no longer decides what a subtitle looks like. It asks
``presentation.compute_blocks`` for positioned primitives and paints them, so
changing the look is a data change rather than a code change, and the web panel
can render the identical geometry.

Window strategy (tkinter limits this, so the choice is deliberate)
------------------------------------------------------------------
tkinter offers two kinds of transparency and they cannot be combined:

* ``-alpha`` gives uniform window transparency, so the *whole* window is
  translucent -- background plate *and* the gaps between blocks.
* ``-transparentcolor`` keys out exactly one colour, giving true transparency
  outside the text, but blocks then have to be fully opaque.

Neither gives per-block opacity. So windows are sized to the drawing that needs
them:

===============  =========================================================
mode             windows
===============  =========================================================
``bar``          one window covering the block, ``-alpha`` = plate opacity
``lines``        one window covering the union of the stacked blocks
``inplace``      one window per line, positioned at that line's box
``panel``        one window, the bilingual history
``hidden``       none
===============  =========================================================

Per-block opacity would need a toolkit that composites properly (Qt), which is
worth remembering if deep theming becomes a goal: this is the one place tkinter
is the limiting factor, not the design.
"""

from __future__ import annotations

import queue
import sys
import time
import tkinter as tk
from dataclasses import replace
from typing import Any, Callable, Sequence

from .events import OverlayStats, OverlayUpdate, TranslatedLine
from .presentation import DrawBlock, PresentationSpec, compute_blocks

__all__ = [
    "Overlay",
    "OverlayStats",
    "OverlayUpdate",
    "set_click_through",
]

# --------------------------------------------------------------------------- #
# platform helpers
# --------------------------------------------------------------------------- #

_IS_WINDOWS = sys.platform.startswith("win")

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

# SetWindowDisplayAffinity values.
WDA_NONE = 0x00000000
#: Window is not captured at all, and whatever is behind it *is* captured.
#: Windows 10 2004+. This is what stops the overlay feeding its own OCR.
WDA_EXCLUDEFROMCAPTURE = 0x00000011
#: Older fallback: the window is captured as a black rectangle. Stops the
#: feedback loop but blinds OCR in that area, so it is second choice.
WDA_MONITOR = 0x00000001

_TRANSPARENT_KEY = "#010203"

#: Floor for interactive panel resizing. Below this the header buttons and the
#: status line start overlapping and the panel stops being usable.
MIN_PANEL_WIDTH = 240
MIN_PANEL_HEIGHT = 140

#: Fonts to fall back to when a spec names a family this machine lacks.
_FALLBACK_FONT_FAMILIES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "SimHei",
    "Noto Sans CJK SC",
    "Arial",
)


def _composite(color: str, background: str | None, alpha: float) -> str:
    """Blend ``color`` over ``background`` at ``alpha``, returning a hex colour.

    tkinter canvas text has no alpha channel -- only a whole toplevel window does --
    so opacity has to be faked by pre-blending the colour against whatever is behind
    it. That is exact when there is a plate, and impossible when the background is
    transparent, which is why the caller passes ``None`` there and the colour is left
    alone rather than being darkened against a guess.

    This is the mechanism the settings page used to advertise with
    ``overlay.dim_low_confidence`` while the painter ignored ``DrawText.opacity``
    entirely: low-confidence lines were drawn exactly as confidently as certain ones.
    For a tool whose output is a guess about pixels, being able to look unsure is not
    a decoration, it is the thing that makes it trustworthy.
    """
    alpha = max(0.0, min(1.0, alpha))
    if alpha >= 1.0 or not background:
        return color
    try:
        fg = tuple(int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        bg = tuple(int(background.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return color
    mixed = tuple(round(f * alpha + b * (1.0 - alpha)) for f, b in zip(fg, bg))
    return "#" + "".join(f"{value:02x}" for value in mixed)


def _toplevel_hwnd(window: tk.Misc) -> int:
    """Return the real top level HWND for a Tk window (Windows only)."""
    if not _IS_WINDOWS:
        return 0
    import ctypes

    window.update_idletasks()
    try:
        child = window.winfo_id()
    except tk.TclError:
        return 0
    user32 = ctypes.windll.user32
    parent = user32.GetParent(child)
    return int(parent) if parent else int(child)


def set_capture_exclusion(window: tk.Misc, enabled: bool = True) -> int:
    """Keep a window out of screen captures while it stays visible on screen.

    Without this the overlay is a feedback loop: it draws subtitles, the capture
    region contains them, OCR reads our own output, that changes the pixels,
    change detection fires again -- and the display stutters. Measured on this
    machine, the overlay occupied 98.9% of a capture over its own rectangle.

    ``WDA_EXCLUDEFROMCAPTURE`` is the right answer rather than a software mask:
    it removes the window from the capture *and* the content behind it becomes
    visible again (measured 98.7%), so OCR reads the real screen instead of being
    blinded. ``WDA_MONITOR`` also stops the loop but renders black, so it is only
    a fallback for Windows older than 10 2004.

    Returns the affinity actually applied.
    """
    if not _IS_WINDOWS:
        return WDA_NONE
    import ctypes
    from ctypes import wintypes

    hwnd = _toplevel_hwnd(window)
    if not hwnd:
        return WDA_NONE

    fn = ctypes.windll.user32.SetWindowDisplayAffinity
    fn.argtypes = [wintypes.HWND, wintypes.DWORD]
    fn.restype = wintypes.BOOL

    if not enabled:
        fn(hwnd, WDA_NONE)
        return WDA_NONE

    if fn(hwnd, WDA_EXCLUDEFROMCAPTURE):
        return WDA_EXCLUDEFROMCAPTURE
    if fn(hwnd, WDA_MONITOR):
        return WDA_MONITOR
    return WDA_NONE


def set_click_through(window: tk.Misc, enabled: bool = True) -> bool:
    """Make a window ignore mouse input.

    Returns True only when the style is **read back and confirmed**, because the
    HWND can be resolved too early: Tk has not created the toplevel parent until
    the window is mapped, so an eager call styles the child window, silently
    succeeds, and leaves the overlay swallowing every click over it.
    """
    if not _IS_WINDOWS:
        return False
    import ctypes
    from ctypes import wintypes

    hwnd = _toplevel_hwnd(window)
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]

    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    new_style = style | WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    if enabled:
        new_style |= WS_EX_TRANSPARENT
    else:
        new_style &= ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)

    applied = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if enabled:
        return bool(applied & WS_EX_TRANSPARENT) and bool(applied & WS_EX_NOACTIVATE)
    return not bool(applied & WS_EX_TRANSPARENT)


def _pick_font(candidates: Sequence[str], default: str = "TkDefaultFont") -> str:
    try:
        import tkinter.font as tkfont

        available = {name.lower() for name in tkfont.families()}
    except Exception:
        return default
    for name in candidates:
        if name.lower() in available:
            return name
    return default


# --------------------------------------------------------------------------- #
# a pooled window used for one visual group
# --------------------------------------------------------------------------- #


class _Surface:
    """A transparent, click-through, always-on-top window plus its canvas."""

    def __init__(self, root: tk.Misc, click_through: bool = True,
                 exclude_from_capture: bool = True) -> None:
        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        try:
            self.window.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self.canvas = tk.Canvas(self.window, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.window.update_idletasks()
        self.click_through = click_through
        self.exclude_from_capture = exclude_from_capture
        self._geometry = ""
        self._bg = ""
        self._hidden = False
        #: Click-through must be applied *after* the window is mapped. Applying
        #: it in the constructor targets the child HWND, because Tk has not
        #: created the toplevel parent yet, and the style silently lands on the
        #: wrong window -- which would leave the overlay swallowing every mouse
        #: click over it.
        self._styled = False
        self.capture_affinity = WDA_NONE

    def place(self, x: int, y: int, width: int, height: int) -> None:
        width = max(1, width)
        height = max(1, height)
        geometry = f"{width}x{height}+{int(x)}+{int(y)}"
        if self._hidden:
            self.window.deiconify()
            self._hidden = False
        if geometry != self._geometry:
            self.window.geometry(geometry)
            self.canvas.configure(width=width, height=height)
            self._geometry = geometry
        if (self.click_through and not self._styled) or (
            self.exclude_from_capture and self.capture_affinity == WDA_NONE
        ):
            self.ensure_click_through()

    def ensure_click_through(self) -> None:
        """(Re)assert click-through and capture exclusion once mapped.

        Two things make this fiddly, both silent:

        * it must not run before the window is mapped, or Tk has not created the
          toplevel parent yet and the style lands on the wrong HWND;
        * Tk **rewrites the extended style** whenever ``-alpha`` or
          ``-transparentcolor`` is set, which clears these bits. So this has to
          be re-asserted after every background change, not just once.

        Capture exclusion is applied here for the same reason, and because a
        window that appears in our own captures creates a feedback loop: the
        overlay's own subtitles get OCR'd, pixels keep changing, and the display
        stutters.
        """
        if not self.window.winfo_ismapped():
            return
        self.window.update_idletasks()
        if self.click_through and not self._styled:
            self._styled = set_click_through(self.window, True)
        if self.exclude_from_capture and self.capture_affinity == WDA_NONE:
            self.capture_affinity = set_capture_exclusion(self.window, True)

    def style(self, background: str, alpha: float | None, transparent_key: str | None) -> None:
        """Apply the background colour and transparency mode.

        Changing these makes Tk rewrite the extended window style, which clears
        the click-through bits, so they are re-asserted afterwards.
        """
        marker = f"{background}|{alpha}|{transparent_key}"
        if marker == self._bg:
            return
        self._bg = marker
        self.canvas.configure(bg=background)
        self.window.configure(bg=background)
        try:
            if transparent_key:
                self.window.attributes("-transparentcolor", transparent_key)
                self.window.attributes("-alpha", 1.0)
            else:
                self.window.attributes("-transparentcolor", "")
                self.window.attributes("-alpha", 1.0 if alpha is None else alpha)
        except tk.TclError:
            pass
        self._styled = False
        self.ensure_click_through()

    def clear(self) -> None:
        self.canvas.delete("all")

    def hide(self) -> None:
        # withdraw() is reliable where resizing to 1x1 is not: a packed canvas
        # immediately overrides the requested geometry
        if not self._hidden:
            try:
                self.window.withdraw()
            except tk.TclError:
                pass
            self._hidden = True

    def destroy(self) -> None:
        try:
            self.window.destroy()
        except tk.TclError:
            pass


# --------------------------------------------------------------------------- #
# overlay
# --------------------------------------------------------------------------- #


class Overlay:
    """Renders the event stream according to a ``PresentationSpec``."""

    def __init__(
        self,
        presentation: PresentationSpec | None = None,
        *,
        click_through: bool = True,
        exclude_from_capture: bool = True,
        on_toggle_pause: Callable[[], bool] | None = None,
        on_reselect: Callable[[Any], None] | None = None,
        screen_size: tuple[int, int] | None = None,
        physical_screen: tuple[int, int] | None = None,
        region_box: tuple[int, int, int, int] | None = None,
        panel_history: int = 12,
        panel_width: int = 540,
        panel_height: int = 320,
        panel_font_size: int = 13,
        font_family: str | None = None,
        dim_low_confidence: bool = True,
    ) -> None:
        self.spec = presentation or PresentationSpec.preset("bar")
        self.click_through = click_through
        self.exclude_from_capture = exclude_from_capture
        self.on_toggle_pause = on_toggle_pause
        #: called with a new Region after an interactive re-selection
        self.on_reselect = on_reselect
        self._screen_size = screen_size
        self._physical_screen = physical_screen
        self._region_box = region_box
        self.panel_history = panel_history
        self.panel_width = panel_width
        self.panel_height = panel_height
        self._panel_font_size = panel_font_size
        self._font_family = font_family
        #: `overlay.dim_low_confidence`. Dimming used to happen whenever coverage fell
        #: below the spec threshold, so the setting could not turn it off -- the switch
        #: was in the config and in the settings page and controlled nothing.
        self.dim_low_confidence = bool(dim_low_confidence)

        self._queue: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=64)
        self._root: tk.Misc | None = None
        #: True when this overlay created the Tk root and must therefore destroy it
        self._owns_root = True
        self._surfaces: list[_Surface] = []
        self._panel: tk.Toplevel | None = None
        self._panel_text: tk.Text | None = None
        self._panel_grip: tk.Label | None = None
        self._status_var: tk.StringVar | None = None
        self._history: list[tuple[str, str]] = []
        self._last_rendered = ""
        self._last_update: OverlayUpdate | None = None
        self._last_blocks: list[DrawBlock] = []
        self._stats = OverlayStats()
        self._closed = False
        self._drag_origin: tuple[int, int] = (0, 0)
        #: (x_root, y_root, width, height) captured when a resize drag starts
        self._resize_origin: tuple[int, int, int, int] | None = None
        #: the previously drawn translation, for the `previous` element role
        self._previous_line = ""
        self._fonts: dict[tuple[int, bool], Any] = {}
        #: Physical pixels per logical (Tk) pixel. Windows display scaling makes
        #: Tk report a *logical* screen while mss and OCR report physical
        #: coordinates, so on a scaled display the two disagree and per-line
        #: boxes have to be converted before layout.
        self.capture_scale = 1.0
        self.renders = 0
        #: hotkey-driven show/hide, independent of the engine's paused state
        self.visible = True
        #: the selector currently open, if any; exposed so a self check can drive
        #: it with synthetic events instead of a human dragging
        self._active_selector: Any = None
        self._visible_before_selection = True
        #: pending Tk after() id, cancelled on close so the pump cannot fire
        #: against a destroyed window and print a confusing Tcl error
        self._after_id: str | None = None

    # -- lifecycle --------------------------------------------------------- #

    def start(self, root: tk.Misc | None = None) -> None:
        """Build the overlay windows.

        ``root`` lets a host application (the desktop UI) share its Tk root. Two
        ``Tk()`` instances in one process each have their own interpreter, and
        widgets from one cannot be placed in the other, so anything that wants
        both a main window and an overlay must share one root.
        """
        if root is None:
            root = tk.Tk()
            root.withdraw()
            self._owns_root = True
        else:
            self._owns_root = False
        self._root = root
        if self._screen_size is None:
            self._screen_size = (root.winfo_screenwidth(), root.winfo_screenheight())
        self._detect_capture_scale()
        if self._font_family is None:
            self._font_family = self._resolve_font_family()
        if self.spec.layout.mode == "panel":
            self._build_panel()
        else:
            self._ensure_surfaces(1)
        self._after_id = root.after(16, self._drain)

    def _detect_capture_scale(self) -> None:
        """Work out physical-vs-logical scaling, and say so when it is not 1:1.

        Tk positions windows in logical pixels; capture and OCR work in physical
        pixels. With Windows display scaling at 150% these differ, and per-line
        boxes silently land in the wrong place unless converted. Better to
        announce it than to draw the translation somewhere unrelated to the text
        it belongs to.
        """
        if not self._physical_screen or not self._screen_size:
            return
        logical_w = self._screen_size[0]
        if logical_w <= 0:
            return
        scale = self._physical_screen[0] / logical_w
        if abs(scale - 1.0) < 0.02:
            self.capture_scale = 1.0
            return
        self.capture_scale = scale
        print(
            f"[overlay] display scaling detected: capture is {self._physical_screen[0]}x"
            f"{self._physical_screen[1]} physical but Tk draws at {logical_w}x"
            f"{self._screen_size[1]} logical (x{scale:.2f}). Per-line boxes are "
            f"converted; in-place layout depends on this."
        )

    def _to_logical_lines(self, update: OverlayUpdate) -> list[Any]:
        """Convert per-line physical boxes into Tk logical coordinates."""
        if self.capture_scale == 1.0 or not update.lines:
            return list(update.lines)
        scale = self.capture_scale
        converted = []
        for line in update.lines:
            if line.box is None:
                converted.append(line)
                continue
            x, y, w, h = line.box
            converted.append(
                replace(
                    line,
                    box=(
                        int(round(x / scale)),
                        int(round(y / scale)),
                        max(1, int(round(w / scale))),
                        max(1, int(round(h / scale))),
                    ),
                )
            )
        return converted

    def _logical_region_origin(self) -> tuple[int, int]:
        if not self._region_box:
            return (0, 0)
        scale = self.capture_scale or 1.0
        return (
            int(round(self._region_box[0] / scale)),
            int(round(self._region_box[1] / scale)),
        )

    def _resolve_font_family(self) -> str:
        """Resolve the spec's font wishes against what this machine actually has.

        A spec naming a missing font should degrade to something readable rather
        than draw blank boxes, so an unavailable family falls through to a
        CJK-capable one.
        """
        available = self._available_fonts()
        preferred = self.spec.resolve_font_family(available)
        if not available or preferred.lower() in {n.lower() for n in available}:
            return preferred
        return _pick_font(_FALLBACK_FONT_FAMILIES)

    def _available_fonts(self) -> list[str]:
        try:
            import tkinter.font as tkfont

            return list(tkfont.families())
        except Exception:
            return []

    def run(self) -> None:
        """Start the Tk main loop (blocks). No-op when a host owns the root."""
        if self._root is None:
            self.start()
        assert self._root is not None
        if not self._owns_root:
            # the host application drives mainloop; the pump is already scheduled
            return
        try:
            self._root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    def close(self, destroy_root: bool | None = None) -> None:
        """Tear down the overlay windows.

        ``destroy_root`` defaults to "only if we created it", so closing the
        overlay cannot pull the floor out from under a host application.
        """
        if self._closed:
            return
        self._closed = True
        if self._after_id is not None and self._root is not None:
            try:
                self._root.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        for surface in self._surfaces:
            surface.destroy()
        self._surfaces = []
        self._destroy_panel()
        should_destroy = self._owns_root if destroy_root is None else destroy_root
        if should_destroy and self._root is not None:
            try:
                self._root.quit()
                self._root.destroy()
            except tk.TclError:
                pass
        self._root = None

    # -- live configuration ------------------------------------------------ #

    def set_region_box(self, box: tuple[int, int, int, int] | None) -> None:
        """Tell the overlay where the capture region is, for in-place layout."""
        self._region_box = box
        self._queue.put_nowait(("redraw", None))

    def apply_presentation(self, spec: PresentationSpec) -> None:
        """Swap the look at runtime. Safe to call from any thread."""
        self._queue.put_nowait(("presentation", spec))

    def toggle_visibility(self) -> None:
        """Hide or show the overlay without stopping recognition.

        Distinct from pausing: recognition keeps running and the stream keeps
        flowing, so a panel or the web view stays live and nothing has to be
        re-warmed when the overlay comes back. Useful when the subtitles are in
        the way for a moment.
        """
        self._queue.put_nowait(("visibility", None))

    def request_reselect(self) -> None:
        """Ask for a new capture region, to be drawn on the Tk thread.

        A hotkey fires on the hotkey thread, but Tk may only be driven from the
        thread that owns it, so this goes through the same queue as everything
        else and is handled in ``_drain``.
        """
        self._queue.put_nowait(("reselect", None))

    def _reselect(self) -> None:
        """Let the user drag a new box, without blocking the event pump.

        The selector is built and left to finish through its callback rather
        than run in a nested ``wait_window()`` loop. A blocking modal inside the
        pump would freeze everything else -- the subtitle display, the stats
        tick -- for as long as the selection is open, and it makes the pump
        impossible to reason about.
        """
        if self._active_selector is not None:
            return  # already selecting; ignore a second request
        from .selector import RegionSelector

        self._visible_before_selection = self.visible
        self.visible = False
        self._hide_surfaces()
        if self._panel is not None:
            try:
                self._panel.withdraw()
            except tk.TclError:
                pass

        try:
            selector = RegionSelector(
                physical_screen=self._physical_screen,
                root=self._root,
                on_done=self._selection_finished,
            )
            #: exposed so a self check can drive the selector without a human
            self._active_selector = selector
            selector.build()
        except Exception as exc:
            self._active_selector = None
            print(f"[overlay] could not open the selector: {exc}", file=sys.stderr)
            self._selection_finished(None)

    def _selection_finished(self, region: Any) -> None:
        """Called by the selector, on the Tk thread, once it closes."""
        self._active_selector = None
        self.visible = self._visible_before_selection
        if self._panel is not None:
            try:
                self._panel.deiconify()
            except tk.TclError:
                pass
        if self.visible and self._last_update is not None:
            self._last_rendered = ""
            self._safe_render(self._last_update)
        if region is None:
            return
        if self.on_reselect is None:
            return
        try:
            self.on_reselect(region)
        except Exception as exc:
            print(f"[overlay] applying the new region failed: {exc}", file=sys.stderr)

    def _toggle_visibility(self) -> None:
        self.visible = not self.visible
        if self.visible:
            if self._last_update is not None:
                # repaint rather than wait for the next subtitle
                self._last_rendered = ""
                self._render(self._last_update)
        else:
            self._hide_surfaces()
            if self._panel is not None:
                try:
                    self._panel.withdraw()
                except tk.TclError:
                    pass
        if self.visible and self._panel is not None:
            try:
                self._panel.deiconify()
            except tk.TclError:
                pass

    # -- thread safe producers -------------------------------------------- #

    def push(self, update: OverlayUpdate) -> None:
        self._put(("update", update))

    def push_stats(self, stats: OverlayStats) -> None:
        self._put(("stats", stats))

    def push_status(self, text: str) -> None:
        self._put(("status", text))

    def _put(self, item: tuple[str, Any]) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # dropping display updates always beats stalling OCR
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(item)
            except queue.Empty:
                pass

    # -- main thread pump -------------------------------------------------- #

    def _drain(self) -> None:
        if self._root is None or self._closed:
            return
        latest: OverlayUpdate | None = None
        repaint = False
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if kind == "update":
                latest = payload
            elif kind == "presentation":
                self._adopt_presentation(payload)
                repaint = True
            elif kind == "redraw":
                repaint = True
            elif kind == "visibility":
                self._safe(self._toggle_visibility)
            elif kind == "reselect":
                self._safe(self._reselect)
            elif kind == "stats":
                self._stats = payload
                self._render_status()
            elif kind == "status":
                if self._status_var is not None:
                    self._status_var.set(str(payload))
        if latest is not None:
            self._safe_render(latest)
            self._safe(self._render_status)
        elif repaint and self._last_update is not None:
            self._safe_render(self._last_update)
        # Always reschedule. A drawing error used to escape this callback and
        # stop the pump for good, which looked like the overlay freezing.
        try:
            self._after_id = self._root.after(16, self._drain)
        except tk.TclError:
            pass

    def _safe(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:
            print(f"[overlay] render failed: {exc}", file=sys.stderr)

    def _safe_render(self, update: OverlayUpdate) -> None:
        self._safe(lambda: self._render(update))

    def _adopt_presentation(self, spec: PresentationSpec) -> None:
        self.spec = spec
        self._fonts.clear()
        # The render guard keys off the subtitle text, which has not changed --
        # so without invalidating it a presentation change would never repaint.
        # That is exactly the bug that makes "customize live" look broken.
        self._last_rendered = ""
        self._last_blocks = []
        if self._root is None:
            return
        if spec.layout.mode == "panel":
            if self._panel is None:
                self._build_panel()
            self._hide_surfaces()
        else:
            if self._panel is not None:
                self._destroy_panel()
            self._ensure_surfaces(1)

    # -- measurement ------------------------------------------------------- #

    def _measure(self, text: str, size: int, bold: bool) -> tuple[int, int]:
        """Single line text measurement, using a real tkinter font."""
        import tkinter.font as tkfont

        key = (size, bold)
        font = self._fonts.get(key)
        if font is None:
            font = tkfont.Font(
                family=self._font_family,
                size=size,
                weight="bold" if bold else "normal",
            )
            self._fonts[key] = font
        return font.measure(text), font.metrics("linespace")

    # -- rendering --------------------------------------------------------- #

    def _ensure_surfaces(self, count: int) -> None:
        assert self._root is not None
        while len(self._surfaces) < count:
            self._surfaces.append(
                _Surface(
                    self._root,
                    click_through=self.click_through,
                    exclude_from_capture=self.exclude_from_capture,
                )
            )

    def _hide_surfaces(self) -> None:
        for surface in self._surfaces:
            surface.hide()

    def _render(self, update: OverlayUpdate) -> None:
        self._last_update = update
        if update.target_text == self._last_rendered and self._last_blocks:
            return
        self._last_rendered = update.target_text

        mode = self.spec.layout.mode
        if mode == "hidden":
            self._hide_surfaces()
            return
        if mode == "panel":
            self._append_history(update)
            return

        origin = self._logical_region_origin()
        dim = (
            self.dim_low_confidence
            and update.coverage < self.spec.confidence.dim_below
        )
        blocks = compute_blocks(
            self.spec,
            source_text=update.source_text,
            target_text=update.target_text,
            lines=self._to_logical_lines(update),
            screen=self._screen_size or (1920, 1080),
            region_origin=origin,
            measure=self._measure,
            trace=update.trace,
            coverage=update.coverage,
            dim=dim,
            previous=self._previous_line,
        )
        self._last_blocks = blocks
        self.renders += 1
        # Remembered after rendering, not before: the scrolling line has to show what
        # was said *last*, so the frame being drawn now must not be its own history.
        if update.target_text.strip() and update.target_text != self._previous_line:
            self._previous_line = update.target_text

        if mode == "inplace":
            self._paint_per_block(blocks)
        else:
            self._paint_group(blocks)

    def _paint_per_block(self, blocks: Sequence[DrawBlock]) -> None:
        """Scattered blocks get their own window so the gaps stay transparent."""
        self._ensure_surfaces(len(blocks))
        for index, surface in enumerate(self._surfaces):
            if index >= len(blocks):
                surface.hide()
                continue
            block = blocks[index]
            surface.place(block.x, block.y, block.width, block.height)
            self._configure_background(surface, block)
            surface.clear()
            self._paint_texts(surface, block, block.x, block.y)

    def _paint_group(self, blocks: Sequence[DrawBlock]) -> None:
        """Contiguous blocks share one window so plate seams do not show."""
        if not blocks:
            self._hide_surfaces()
            return
        left = min(b.x for b in blocks)
        top = min(b.y for b in blocks)
        right = max(b.right for b in blocks)
        bottom = max(b.bottom for b in blocks)
        self._ensure_surfaces(1)
        surface = self._surfaces[0]
        surface.place(left, top, right - left, bottom - top)
        self._configure_background(surface, blocks[0])
        surface.clear()
        for block in blocks:
            self._paint_texts(surface, block, left, top)
        for extra in self._surfaces[1:]:
            extra.hide()

    def _configure_background(self, surface: _Surface, block: DrawBlock) -> None:
        background = block.background
        if background is None or background.kind == "none":
            surface.style(_TRANSPARENT_KEY, None, _TRANSPARENT_KEY)
        else:
            surface.style(background.color, background.opacity, None)

    def _paint_texts(
        self, surface: _Surface, block: DrawBlock, origin_x: int, origin_y: int
    ) -> None:
        """Draw a block's texts, translating screen coordinates to canvas ones.

        ``origin_x``/``origin_y`` are the screen coordinates of the window's
        top-left corner -- the block's own corner for a per-block window, or the
        group's top-left when several blocks share one window.

        This parameter used to be an additive ``dx``/``dy`` offset applied on top of
        ``text.x - block.x``, which double-counted the origin: `text.x` and
        `block.x` are both absolute screen coordinates, so the subtraction already
        yields the in-block offset. Group painting therefore drew its text at
        roughly minus twice the origin, i.e. entirely outside the canvas. The
        window background still painted, so the result was a black rectangle with
        no text in it -- for bar, bare, minimal and lines alike.
        """
        canvas = surface.canvas
        # The plate colour, when there is one, is what a faded glyph is blended
        # against. A transparent background gives nothing to blend with, so opacity
        # is left alone there -- dimming towards black would darken text over a video
        # and read as a rendering fault rather than as uncertainty.
        plate = None
        if block.background is not None and block.background.kind != "none":
            plate = block.background.color
        for text in block.texts:
            font = (self._font_family, text.size, "bold" if text.bold else "normal")
            x = text.x - origin_x
            y = text.y - origin_y
            # fill is supplied per pass, so it must not be in the shared options
            options: dict[str, Any] = {
                "text": text.text,
                "font": font,
                "anchor": "nw",
                "justify": "left",
                "width": max(1, block.width),
            }
            fill = _composite(text.color, plate, text.opacity)
            if text.outline_color and text.outline_width:
                # tkinter has no text stroke, so draw a copy underneath in each
                # surrounding direction
                outline = _composite(text.outline_color, plate, text.opacity)
                radius = text.outline_width
                for ox in range(-radius, radius + 1):
                    for oy in range(-radius, radius + 1):
                        if ox == 0 and oy == 0:
                            continue
                        canvas.create_text(
                            x + ox, y + oy, fill=outline, **options
                        )
            canvas.create_text(x, y, fill=fill, **options)

    # -- panel mode -------------------------------------------------------- #

    def _build_panel(self) -> None:
        assert self._root is not None
        panel = tk.Toplevel(self._root)
        panel.overrideredirect(True)
        panel.attributes("-topmost", True)
        try:
            panel.attributes("-alpha", self.spec.background.opacity or 0.93)
            panel.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        panel.configure(bg=self.spec.background.color)

        width = int((self._screen_size or (1920, 1080))[0] * 0.28)
        # `panel_width` used to be stored and never read, so config.yaml's
        # overlay.panel_width had no effect whatsoever and the panel was always
        # 28% of the screen. A width of 0 means "pick one from the screen".
        if self.panel_width and self.panel_width > 0:
            width = int(min(self.panel_width, (self._screen_size or (1920, 1080))[0]))
        height = int(self.panel_height) if self.panel_height else 320
        width = max(MIN_PANEL_WIDTH, width)
        height = max(MIN_PANEL_HEIGHT, height)
        panel.geometry(f"{width}x{height}+24+24")
        panel.update_idletasks()
        # the panel sits on screen too, so it is just as capable of feeding its
        # own OCR as the subtitle bar is
        if self.exclude_from_capture:
            applied = set_capture_exclusion(panel, True)
            if applied == WDA_NONE:
                print("[overlay] warning: the panel could not be excluded from capture")
            elif applied == WDA_MONITOR:
                print(
                    "[overlay] note: this Windows build only supports WDA_MONITOR, so "
                    "the panel appears black in captures"
                )

        header = tk.Frame(panel, bg="#1b2229", height=28)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        title = tk.Label(
            header,
            text="Project Watashi  ·  实时对照",
            bg="#1b2229",
            fg="#d8e2ea",
            font=(self._font_family, self._panel_font_size, "bold"),
            anchor="w",
            padx=10,
        )
        title.pack(side="left", fill="y")
        close_btn = tk.Label(
            header, text="✕", bg="#1b2229", fg="#8fa3b3",
            font=(self._font_family, self._panel_font_size), padx=10, cursor="hand2",
        )
        close_btn.pack(side="right", fill="y")
        close_btn.bind("<Button-1>", lambda _e: self.close())
        pause_btn = tk.Label(
            header, text="暂停", bg="#1b2229", fg="#8fa3b3",
            font=(self._font_family, self._panel_font_size), padx=8, cursor="hand2",
        )
        pause_btn.pack(side="right", fill="y")
        pause_btn.bind("<Button-1>", lambda _e: self._toggle_pause())
        for widget in (header, title):
            widget.bind("<Button-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._on_drag)

        self._status_var = tk.StringVar(value="等待识别…")
        status = tk.Label(
            panel,
            textvariable=self._status_var,
            bg="#0b0f13",
            fg="#6f8496",
            font=(self._font_family, max(8, self._panel_font_size - 3)),
            anchor="w",
            justify="left",
            padx=10,
            pady=3,
        )
        status.pack(fill="x", side="bottom")

        body = tk.Frame(panel, bg=self.spec.background.color)
        body.pack(fill="both", expand=True)

        # A resize grip, drawn by hand.
        #
        # `overrideredirect(True)` is what makes the panel borderless and free of a
        # title bar, and it also means Windows provides no resize frame at all --
        # which is why the panel could be moved but never resized. ttk.Sizegrip
        # cannot help here either: it asks the window manager to start a resize, and
        # there is no window manager frame to drag. So the grip is a placed label
        # with its own drag handlers. Placed (not packed) so it floats over the
        # status strip in the corner instead of stealing layout space from it.
        grip = tk.Label(
            panel,
            text="◢",
            bg="#1b2229",
            fg="#6f8496",
            cursor="bottom_right_corner",
            font=(self._font_family, 11),
            padx=1,
            pady=0,
        )
        grip.place(relx=1.0, rely=1.0, anchor="se")
        grip.bind("<Button-1>", self._start_resize)
        grip.bind("<B1-Motion>", self._on_resize)
        self._panel_grip = grip
        text = tk.Text(
            body,
            bg=self.spec.background.color,
            fg="#e6eef5",
            font=(self._font_family, self._panel_font_size),
            wrap="word",
            bd=0,
            highlightthickness=0,
            padx=10,
            pady=6,
            insertwidth=0,
        )
        text.pack(fill="both", expand=True)
        for element in self.spec.ordered_elements():
            text.tag_configure(
                element.role,
                foreground=element.color,
                font=(
                    self._font_family,
                    element.font.size,
                    "bold" if element.font.bold else "normal",
                ),
            )
        text.tag_configure("meta", foreground="#5f7386")
        text.tag_configure("gap", spacing1=6)
        text.configure(state="disabled")

        self._panel = panel
        self._panel_text = text

    def _destroy_panel(self) -> None:
        if self._panel is not None:
            try:
                self._panel.destroy()
            except tk.TclError:
                pass
        self._panel = None
        self._panel_text = None
        self._panel_grip = None
        self._status_var = None
        self._resize_origin = None

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_origin = (event.x_root, event.y_root)

    def _on_drag(self, event: tk.Event) -> None:
        if self._panel is None or self._drag_origin is None:
            return
        dx = event.x_root - self._drag_origin[0]
        dy = event.y_root - self._drag_origin[1]
        self._panel.geometry(f"+{self._panel.winfo_x() + dx}+{self._panel.winfo_y() + dy}")
        self._drag_origin = (event.x_root, event.y_root)

    def _start_resize(self, event: tk.Event) -> None:
        if self._panel is None:
            return
        # Remember the size at grab time and apply the total delta each motion,
        # rather than accumulating per-event deltas. Accumulation drifts when Tk
        # coalesces motion events, and it cannot enforce a floor properly.
        self._resize_origin = (
            event.x_root,
            event.y_root,
            self._panel.winfo_width(),
            self._panel.winfo_height(),
        )

    def _on_resize(self, event: tk.Event) -> None:
        if self._panel is None or self._resize_origin is None:
            return
        x0, y0, w0, h0 = self._resize_origin
        width = max(MIN_PANEL_WIDTH, w0 + (event.x_root - x0))
        height = max(MIN_PANEL_HEIGHT, h0 + (event.y_root - y0))
        self._panel.geometry(f"{width}x{height}")
        # Persist, so a panel that is rebuilt (a presentation switch, a profile
        # change) keeps the size the user dragged to instead of snapping back.
        self.panel_width = width
        self.panel_height = height

    def set_panel_size(self, width: int, height: int) -> tuple[int, int]:
        """Resize the panel programmatically. Returns the size actually applied."""
        width = max(MIN_PANEL_WIDTH, int(width))
        height = max(MIN_PANEL_HEIGHT, int(height))
        self.panel_width = width
        self.panel_height = height
        if self._panel is not None:
            try:
                self._panel.geometry(f"{width}x{height}")
            except tk.TclError:
                pass
        return width, height

    def _toggle_pause(self) -> None:
        if self.on_toggle_pause is None:
            return
        paused = self.on_toggle_pause()
        button = None
        if self._panel is not None:
            for child in self._panel.winfo_children():
                for sub in child.winfo_children():
                    if isinstance(sub, tk.Label) and sub.cget("text") in ("暂停", "继续"):
                        button = sub
            if button is not None:
                button.configure(text="继续" if paused else "暂停")

    def _append_history(self, update: OverlayUpdate) -> None:
        widget = self._panel_text
        if widget is None:
            return
        entries = update.lines or [
            TranslatedLine(source=update.source_text, target=update.target_text)
        ]
        for entry in entries:
            source = entry.source.strip()
            target = entry.target.strip()
            if not target:
                continue
            if self._history and self._history[-1] == (source, target):
                continue
            if self._history and self._history[-1][0] == source:
                self._history[-1] = (source, target)
            else:
                self._history.append((source, target))
        if len(self._history) > self.panel_history:
            self._history = self._history[-self.panel_history :]

        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for index, (source, target) in enumerate(self._history):
            if index:
                widget.insert("end", "\n", ("gap",))
            if source and self.spec.element("source"):
                widget.insert("end", source + "\n", ("source",))
            widget.insert("end", target + "\n", ("target",))
            if index == len(self._history) - 1:
                origin = "模型" if update.refined else "语料库"
                widget.insert(
                    "end",
                    f"{origin} · 延迟 {update.latency_ms:.0f} ms"
                    f" · 覆盖率 {update.coverage * 100:.0f}%\n",
                    ("meta",),
                )
        widget.configure(state="disabled")
        widget.see("end")

    def _render_status(self) -> None:
        if self._status_var is None:
            return
        s = self._stats
        parts = [
            s.backend or "corpus+rules",
            f"{s.fps:.1f} FPS",
            f"OCR {s.ocr_ms:.0f}ms",
            f"总 {s.total_ms:.0f}ms",
            f"词库 {s.corpus_entries}",
            f"规则 {s.rules}",
            f"缓存 {s.cache_hit_rate * 100:.0f}%",
            f"跳过 {s.skipped}",
        ]
        if s.memory_mib:
            parts.append(f"内存 {s.memory_mib:.0f}MiB")
        if s.refinements or s.refinements_dropped:
            # superseded requests are dropped on purpose (newest wins), but the
            # count is worth showing rather than hiding
            extra = f"(丢弃 {s.refinements_dropped})" if s.refinements_dropped else ""
            parts.append(f"精修 {s.refinements}{extra}")
        if s.paused:
            parts.insert(0, "⏸ 已暂停")
        self._status_var.set("  ·  ".join(parts))
