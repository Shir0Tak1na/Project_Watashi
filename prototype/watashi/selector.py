"""Interactive drag-to-select for the capture region.

Two things this gets right that an earlier version did not, both of which are
invisible until you measure:

**Logical versus physical coordinates.** Tk reports mouse positions in
DPI-scaled *logical* pixels; the capture library works in *physical* pixels. On a
150% display the two differ by 1.5x. The old selector handed logical coordinates
straight to the capturer, so a drag the user saw as (400,400)-(1200,700) was
captured at (400,400) with size 800x300 -- off by about 199 px and short by
399x149. The selection is converted on release.

**It is testable.** The event handlers are methods, so a synthetic drag
(``event_generate`` with ``rootx``/``rooty``) can drive the whole thing with no
human involved. Otherwise the only way to know this works is to try it by hand,
which is how the coordinate bug survived.

The selector covers the primary monitor, matching what ``-fullscreen`` does. A
region on a secondary monitor is out of reach for a drag; pass ``--region``
explicitly there.
"""

from __future__ import annotations

from typing import Any, Callable

from .capture import Region, display_scale, list_monitors, logical_to_physical

MIN_SELECTION = 20


class RegionSelector:
    """A dimmed full-screen overlay you drag a rectangle on."""

    def __init__(
        self,
        physical_screen: tuple[int, int] | None = None,
        root: Any | None = None,
        on_done: Callable[[Region | None], None] | None = None,
    ) -> None:
        self._physical = physical_screen
        self._root = root
        self._owns_root = root is None
        self.on_done = on_done
        self.result: Region | None = None
        self.logical_box: tuple[int, int, int, int] | None = None
        self.scale = 1.0
        self._start: tuple[int, int] | None = None
        self._rect_id: int | None = None
        self._canvas: Any = None
        self._window: Any = None

    # -- construction ------------------------------------------------------ #

    def build(self) -> Any:
        """Create the full-screen window. Returns the Tk window."""
        import tkinter as tk

        if self._root is None:
            self._root = tk.Tk()
            self._root.withdraw()
        root = self._root

        window = tk.Toplevel(root)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        try:
            window.attributes("-alpha", 0.35)
        except tk.TclError:
            pass
        window.configure(bg="black", cursor="crosshair")

        screen_w = window.winfo_screenwidth()
        screen_h = window.winfo_screenheight()
        window.geometry(f"{screen_w}x{screen_h}+0+0")

        self.scale = display_scale(
            (screen_w, screen_h),
            self._physical if self._physical is not None else self._detect_physical(),
        )

        canvas = tk.Canvas(window, bg="black", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)

        hint = "拖拽选择要识别的屏幕区域   ·   Esc 取消"
        if self.scale != 1.0:
            hint += f"   （显示缩放 {self.scale:.2f}x，已自动换算）"
        canvas.create_text(
            screen_w // 2, 48, text=hint, fill="#ffffff",
            font=("Microsoft YaHei UI", 18),
        )
        canvas.create_text(
            screen_w // 2, 84,
            text="选择窗口可用 --window，不必手动框选",
            fill="#9fb0bd", font=("Microsoft YaHei UI", 12),
        )

        canvas.bind("<ButtonPress-1>", self.on_press)
        canvas.bind("<B1-Motion>", self.on_drag)
        canvas.bind("<ButtonRelease-1>", self.on_release)
        window.bind("<Escape>", self.on_cancel)

        self._canvas = canvas
        self._window = window
        window.update_idletasks()
        window.update()
        # An overrideredirect window does not take focus by default, so a plain
        # `<Escape>` binding never fires and the user is left in a full-screen
        # modal with no way out. Forcing focus makes Escape work.
        try:
            window.focus_force()
        except tk.TclError:
            pass
        return window

    def _detect_physical(self) -> tuple[int, int] | None:
        try:
            monitor = list_monitors()[1]
            return (int(monitor["width"]), int(monitor["height"]))
        except Exception:
            return None

    # -- interaction ------------------------------------------------------- #

    def on_press(self, event: Any) -> None:
        self._start = (int(event.x_root), int(event.y_root))
        if self._rect_id is not None:
            self._canvas.delete(self._rect_id)
        self._rect_id = self._canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="#4ea1ff", width=2, fill="#4ea1ff",
        )

    def on_drag(self, event: Any) -> None:
        if self._start is None or self._rect_id is None:
            return
        origin_x = self._window.winfo_rootx()
        origin_y = self._window.winfo_rooty()
        self._canvas.coords(
            self._rect_id,
            self._start[0] - origin_x,
            self._start[1] - origin_y,
            event.x,
            event.y,
        )

    def on_release(self, event: Any) -> None:
        if self._start is None:
            self._finish(None)
            return
        x0, y0 = self._start
        x1, y1 = int(event.x_root), int(event.y_root)
        logical = (min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
        self.logical_box = logical

        if logical[2] < MIN_SELECTION or logical[3] < MIN_SELECTION:
            # a stray click, not a drag: treat it as a cancel rather than
            # producing a degenerate region that would capture nothing
            self._finish(None)
            return

        physical = logical_to_physical(logical, self.scale)
        self._finish(Region(physical[0], physical[1], physical[2], physical[3]))

    def on_cancel(self, _event: Any = None) -> None:
        self._finish(None)

    def _finish(self, region: Region | None) -> None:
        self.result = region
        if self.on_done is not None:
            self.on_done(region)
        self.close()

    def close(self) -> None:
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
            self._window = None
        if self._owns_root and self._root is not None:
            try:
                self._root.destroy()
            except Exception:
                pass
            self._root = None

    # -- blocking convenience --------------------------------------------- #

    def run(self) -> Region | None:
        """Build the selector and block until the user finishes."""
        window = self.build()
        root = self._root
        try:
            # the window is a Toplevel, so the mainloop runs on the root; the
            # handler destroys the window and we stop when it is gone
            window.wait_window()
            if root is not None and self._owns_root:
                root.quit()
        except Exception:
            pass
        return self.result


def select_region(
    physical_screen: tuple[int, int] | None = None, root: Any | None = None
) -> Region | None:
    """Show the selector and return the chosen region in physical pixels."""
    return RegionSelector(physical_screen=physical_screen, root=root).run()
