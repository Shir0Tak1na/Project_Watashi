"""Windows window enumeration and geometry, for capturing a window rather than a
fixed rectangle.

Capturing "the window the video is playing in" instead of "the rectangle I
dragged once" matters for two reasons:

* **The region follows the window.** Move or resize it and the capture moves
  with it, instead of silently reading whatever happens to be at the old
  coordinates.
* **It cannot drift onto our own overlay.** A dragged rectangle is a snapshot of
  a moment; a window reference stays correct.

Only the *client* area is reported, because that is the content: a title bar and
window borders carry nothing worth OCR-ing and would only add noise and shift the
per-line boxes.

Windows only. Everything here degrades to "unavailable" elsewhere rather than
raising, so the rest of the program does not have to care.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

_IS_WINDOWS = sys.platform.startswith("win")

#: Windows that are smaller than this are almost always tool windows, tray
#: helpers or invisible scaffolding, not something a user wants to translate.
MIN_WIDTH = 120
MIN_HEIGHT = 80

_user32 = ctypes.windll.user32 if _IS_WINDOWS else None
_kernel32 = ctypes.windll.kernel32 if _IS_WINDOWS else None


@dataclass
class WindowInfo:
    """A top-level window worth offering to the user."""

    hwnd: int
    title: str
    class_name: str
    pid: int
    process: str
    #: client area in screen coordinates: (x, y, width, height)
    rect: tuple[int, int, int, int]
    minimized: bool

    @property
    def size_label(self) -> str:
        return f"{self.rect[2]}x{self.rect[3]}"

    def label(self) -> str:
        proc = f" [{self.process}]" if self.process else ""
        return f"{self.title}{proc}  ({self.size_label})"


def _process_name(pid: int) -> str:
    """Best-effort executable name for a pid, without extra dependencies."""
    if not _IS_WINDOWS:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(260)
        buffer = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value.rsplit("\\", 1)[-1]
    except Exception:
        pass
    finally:
        _kernel32.CloseHandle(handle)
    return ""


def client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """The window's client area in screen coordinates, or None if unavailable."""
    if not _IS_WINDOWS or not hwnd:
        return None
    rect = wintypes.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    # GetClientRect gives client coordinates, so the origin is converted back to
    # screen space before it is usable as a capture rectangle.
    point = wintypes.POINT(0, 0)
    if not _user32.ClientToScreen(hwnd, ctypes.byref(point)):
        return None
    return (int(point.x), int(point.y), int(width), int(height))


def is_alive(hwnd: int) -> bool:
    return bool(_IS_WINDOWS and hwnd and _user32.IsWindow(hwnd))


def is_minimized(hwnd: int) -> bool:
    return bool(_IS_WINDOWS and hwnd and _user32.IsIconic(hwnd))


def is_visible(hwnd: int) -> bool:
    return bool(_IS_WINDOWS and hwnd and _user32.IsWindowVisible(hwnd))


#: Values returned by GetWindowDisplayAffinity, and the ones that mean "this window is
#: hidden from screen capture".
WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001
WDA_EXCLUDEFROMCAPTURE = 0x00000011
_CAPTURE_EXCLUDED = (WDA_MONITOR, WDA_EXCLUDEFROMCAPTURE)


def capture_affinity(hwnd: int) -> int:
    """How this window relates to screen capture, as Windows sees it."""
    if not _IS_WINDOWS or not hwnd:
        return WDA_NONE
    value = wintypes.DWORD(0)
    if _user32.GetWindowDisplayAffinity(wintypes.HWND(hwnd), ctypes.byref(value)):
        return int(value.value)
    return WDA_NONE


def is_capture_excluded(hwnd: int) -> bool:
    return capture_affinity(hwnd) in _CAPTURE_EXCLUDED


def list_windows(
    visible_only: bool = True,
    min_size: tuple[int, int] = (MIN_WIDTH, MIN_HEIGHT),
    exclude_pid: int | None = None,
) -> list[WindowInfo]:
    """Enumerate top-level windows, largest title-bearing ones first.

    ``exclude_pid`` is how the caller filters out its own windows: offering the
    overlay or the panel as capture targets would rebuild the very feedback loop
    the capture exclusion exists to prevent.
    """
    if not _IS_WINDOWS:
        return []

    results: list[WindowInfo] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, _lparam: int) -> bool:
        if visible_only and not is_visible(hwnd):
            return True
        if _user32.GetWindow(hwnd, 4):  # GW_OWNER: skip owned popups
            return True

        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if not title:
            return True

        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if exclude_pid and pid.value == exclude_pid:
            return True

        rect = client_rect(hwnd)
        if rect is None:
            return True
        if rect[2] < min_size[0] or rect[3] < min_size[1]:
            return True

        class_buffer = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, class_buffer, 256)

        results.append(
            WindowInfo(
                hwnd=int(hwnd),
                title=title,
                class_name=class_buffer.value,
                pid=int(pid.value),
                process=_process_name(int(pid.value)),
                rect=rect,
                minimized=is_minimized(hwnd),
            )
        )
        return True

    _user32.EnumWindows(WNDENUMPROC(callback), 0)

    # UWP apps surface twice -- once through ApplicationFrameHost and once through
    # the real process -- with the same title and a client area differing by a
    # pixel or two. Collapse those so the list a user reads has no phantoms.
    # Sizes are bucketed coarsely: exact equality misses the 1px difference.
    seen: set[tuple[str, int, int, int, int]] = set()
    deduped: list[WindowInfo] = []
    for window in results:
        x, y, width, height = window.rect
        key = (window.title, x, y, width // 16, height // 16)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(window)

    deduped.sort(key=lambda w: w.rect[2] * w.rect[3], reverse=True)
    return deduped


def own_ui_over(
    region: Any,
    marker: str = "Project Watashi",
    own_pid: int | None = None,
    min_overlap: float = 0.15,
    windows: Sequence["WindowInfo"] | None = None,
) -> list[tuple["WindowInfo", float]]:
    """Windows showing *this* application that sit inside the capture region.

    The feedback loop this prevents is old and specific: the engine photographs a
    rectangle of the screen, and if one of our own windows is in that rectangle it reads
    its own output -- a subtitle bar showing the previous translation, or the settings
    window with a page of dense text that changes every time a counter ticks. Change
    detection then fires continuously and the recogniser is never idle, which the user
    experiences as the program stuttering from the moment it starts.

    The overlay solves it for itself by asking Windows to exclude it from capture. That
    cannot work for a browser showing the web panel: the window belongs to another
    process, so there is nothing to ask. This is the other half -- noticing, and saying
    so, rather than OCR-ing our own settings page.

    Matched two ways, because the two cases are different: a window created by this very
    process (``own_pid``), and any window whose title carries the application's name --
    which is what a browser tab holding the panel looks like from the outside. A terminal
    window running the CLI is neither: it belongs to the terminal's own process.

    ``windows`` lets a caller (or a check) supply the list instead of enumerating the
    desktop, which is what makes the filtering testable without a screen.
    """
    if windows is None:
        windows = list_windows(visible_only=True, min_size=(64, 48))
    found: list[tuple[WindowInfo, float]] = []
    needle = marker.strip().lower()
    for window in windows:
        if window.minimized:
            continue
        # A window that already excludes itself from capture cannot appear in the frame,
        # so holding the engine for it would be a false alarm -- and the overlay and the
        # desktop window both set that flag, which is the case this distinction exists for.
        if is_capture_excluded(window.hwnd):
            continue
        ours_by_process = own_pid is not None and window.pid == own_pid
        ours_by_title = bool(needle) and needle in window.title.lower()
        if not (ours_by_process or ours_by_title):
            continue
        x, y, width, height = window.rect
        ratio = region.overlap_ratio(type(region)(x, y, width, height))
        if ratio >= min_overlap:
            found.append((window, ratio))
    found.sort(key=lambda pair: pair[1], reverse=True)
    return found


def resolve_window(
    spec: str,
    exclude_pid: int | None = None,
) -> tuple[WindowInfo | None, str]:
    """Find a window from ``"3"`` (list index) or a title substring.

    Returns ``(window, error)`` with exactly one of the two set, so the caller can
    report *why* nothing matched instead of failing silently.
    """
    windows = list_windows(exclude_pid=exclude_pid)
    if not windows:
        return None, "no capturable windows found"

    text = spec.strip()
    if not text:
        return None, "empty window selector"

    if text.lstrip("-").isdigit():
        index = int(text)
        if 0 <= index < len(windows):
            return windows[index], ""
        return None, f"index {index} is out of range (0..{len(windows) - 1})"

    lowered = text.lower()
    exact = [w for w in windows if w.title.lower() == lowered]
    if len(exact) == 1:
        return exact[0], ""
    partial = [w for w in windows if lowered in w.title.lower()]
    if len(partial) == 1:
        return partial[0], ""
    if len(partial) > 1:
        names = ", ".join(f"{i}:{w.title[:28]}" for i, w in enumerate(partial[:5]))
        return None, f"{len(partial)} windows match {spec!r}: {names}"
    return None, f"no window title contains {spec!r}"


def describe_windows(windows: Iterable[WindowInfo]) -> list[str]:
    lines = []
    for index, window in enumerate(windows):
        marker = " [minimized]" if window.minimized else ""
        lines.append(f"[{index:2d}] {window.label()}{marker}")
    return lines
