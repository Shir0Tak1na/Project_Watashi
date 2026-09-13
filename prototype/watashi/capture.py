"""Screen region capture with change detection.

Two things matter for the "real time, low latency first" requirement (R4):

1. **Capture only the region we care about.** Full screen capture plus a crop
   wastes time; ``mss`` grabs the sub rectangle directly.
2. **Do not OCR unchanged frames.** A change detector compares a tiny grayscale
   signature of each frame against the previous one and skips OCR entirely when
   nothing moved. Without this, a "continuously monitoring" overlay burns a full
   CPU core rendering identical subtitles.

``mss`` instances are not documented as thread safe, so each thread gets its own
through a ``threading.local``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class Region:
    """A rectangle in virtual screen coordinates."""

    x: int
    y: int
    width: int
    height: int

    @property
    def valid(self) -> bool:
        return self.width > 0 and self.height > 0

    def as_mss(self) -> dict[str, int]:
        return {"left": self.x, "top": self.y, "width": self.width, "height": self.height}

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.x},{self.y},{self.width},{self.height}"

    @classmethod
    def parse(cls, spec: str) -> "Region":
        """Parse ``"x,y,w,h"`` (also accepts ``x y w h``)."""
        parts = [p for p in spec.replace(",", " ").split() if p]
        if len(parts) != 4:
            raise ValueError(f"expected 'x,y,w,h', got {spec!r}")
        x, y, w, h = (int(float(p)) for p in parts)
        return cls(x, y, w, h)

    @classmethod
    def bottom_strip(
        cls, monitor: dict[str, int], height_ratio: float = 0.18, margin: float = 0.06
    ) -> "Region":
        """Default subtitle area: a strip across the lower part of a monitor."""
        height = max(60, int(monitor["height"] * height_ratio))
        width = int(monitor["width"] * (1.0 - 2 * margin))
        x = monitor["left"] + int(monitor["width"] * margin)
        y = monitor["top"] + monitor["height"] - height - int(monitor["height"] * 0.08)
        return cls(x, y, width, height)


def list_monitors() -> list[dict[str, int]]:
    """Return the available monitors (index 0 is the union of all of them)."""
    import mss

    with mss.MSS() as sct:
        return [dict(m) for m in sct.monitors]


def display_scale(
    logical: tuple[int, int],
    physical: tuple[int, int] | None,
    tolerance: float = 0.02,
) -> float:
    """Physical pixels per logical pixel, for converting UI coordinates to capture ones.

    Tk reports a DPI-scaled *logical* screen; capture and OCR work in *physical*
    pixels. At 150% display scaling those differ by 1.5x, so any coordinate
    crossing between the two has to be converted or it lands in the wrong place.

    Returns 1.0 when the two agree, when either size is unknown, or when the
    ratio is close enough to 1 to be rounding noise.
    """
    if not physical or logical[0] <= 0 or physical[0] <= 0:
        return 1.0
    scale = physical[0] / logical[0]
    return 1.0 if abs(scale - 1.0) < tolerance else scale


def logical_to_physical(
    box: tuple[int, int, int, int], scale: float
) -> tuple[int, int, int, int]:
    """Convert a logical ``(x, y, w, h)`` into physical pixels."""
    if scale == 1.0:
        return box
    x, y, width, height = box
    return (
        int(round(x * scale)),
        int(round(y * scale)),
        max(1, int(round(width * scale))),
        max(1, int(round(height * scale))),
    )


def physical_to_logical(
    box: tuple[int, int, int, int], scale: float
) -> tuple[int, int, int, int]:
    """Convert a physical ``(x, y, w, h)`` into logical pixels."""
    if scale == 1.0:
        return box
    x, y, width, height = box
    return (
        int(round(x / scale)),
        int(round(y / scale)),
        max(1, int(round(width / scale))),
        max(1, int(round(height / scale))),
    )


class RegionCapturer:
    """Grabs a screen region as a BGR ``numpy`` array."""

    #: What this capturer is bound to. The session branches on this rather than on
    #: ``isinstance`` or on duck-typing ``set_region``: ``WindowCapturer`` has a
    #: ``set_region`` too (a documented no-op, because a window owns its own
    #: rectangle), so probing for the method silently picked the wrong behaviour
    #: and a dragged region was ignored while the window kept being followed.
    KIND = "region"

    def __init__(self, region: Region | None = None, monitor: int = 1) -> None:
        self.monitor_index = monitor
        self._local = threading.local()
        self._region = region
        if region is None:
            monitors = list_monitors()
            if monitor >= len(monitors):
                raise ValueError(
                    f"monitor {monitor} not available (found {len(monitors) - 1})"
                )
            self._region = Region.bottom_strip(monitors[monitor])

    @property
    def region(self) -> Region:
        assert self._region is not None
        return self._region

    def set_region(self, region: Region) -> None:
        self._region = region

    def _sct(self) -> Any:
        sct = getattr(self._local, "sct", None)
        if sct is None:
            import mss

            sct = mss.MSS()
            self._local.sct = sct
        return sct

    def grab(self) -> np.ndarray:
        """Capture the region. Returns a BGR ``uint8`` array of shape (h, w, 3)."""
        shot = self._sct().grab(self.region.as_mss())
        # mss returns BGRA; dropping alpha here is cheaper than a cvtColor pass
        frame = np.asarray(shot, dtype=np.uint8)[:, :, :3]
        return np.ascontiguousarray(frame)

    def close(self) -> None:
        sct = getattr(self._local, "sct", None)
        if sct is not None:
            try:
                sct.close()
            except Exception:
                pass
            self._local.sct = None


class WindowUnavailable(RuntimeError):
    """The tracked window is gone, minimised, or too small to read."""


class WindowCapturer:
    """Captures a window's client area and follows it as it moves or resizes.

    The rectangle is recomputed on every grab rather than resolved once, which is
    the whole difference from a dragged region: move the video player and the
    capture moves with it instead of silently reading whatever now occupies the
    old coordinates.

    A minimised or closed window raises :class:`WindowUnavailable`; the pipeline
    already treats a failed capture as a recoverable error and back off, which is
    better than quietly OCR-ing a stale frame.
    """

    #: See ``RegionCapturer.KIND``. A window capturer can never honour a fixed
    #: rectangle, so the session swaps the capture source instead of setting one.
    KIND = "window"

    def __init__(
        self,
        hwnd: int = 0,
        spec: str = "",
        exclude_pid: int | None = None,
        title: str = "",
        sub_region: tuple[float, float, float, float] | None = None,
    ) -> None:
        self.hwnd = int(hwnd)
        self.spec = spec
        self.title = title
        self.exclude_pid = exclude_pid
        #: Fraction of the window's client area to capture, as
        #: ``(left, top, right, bottom)`` in 0..1. A whole window holding a dense
        #: page of text costs ~100 ms of recognition per text box, so capturing
        #: only the part that matters (a subtitle strip, a video area) is what
        #: makes window capture practical. None means the whole client area.
        self.sub_region = sub_region
        self._local = threading.local()
        self._last_rect: Region | None = None
        if not self.hwnd and spec:
            self.resolve()

    @staticmethod
    def parse_sub_region(spec: str) -> tuple[float, float, float, float]:
        """Parse ``"0,0.6,1,1"`` into fractions, validating the range."""
        parts = [p for p in spec.replace(",", " ").split() if p]
        if len(parts) != 4:
            raise ValueError(f"expected 'left,top,right,bottom' as fractions, got {spec!r}")
        values = [float(p) for p in parts]
        if any(v < 0.0 or v > 1.0 for v in values):
            raise ValueError(f"fractions must be within 0..1, got {spec!r}")
        left, top, right, bottom = values
        if right <= left or bottom <= top:
            raise ValueError(f"right/bottom must exceed left/top, got {spec!r}")
        return (left, top, right, bottom)

    def _apply_sub_region(self, rect: Region) -> Region:
        if self.sub_region is None:
            return rect
        left, top, right, bottom = self.sub_region
        x = rect.x + int(rect.width * left)
        y = rect.y + int(rect.height * top)
        width = max(1, int(rect.width * (right - left)))
        height = max(1, int(rect.height * (bottom - top)))
        return Region(x, y, width, height)

    # -- resolution -------------------------------------------------------- #

    def resolve(self) -> "WindowCapturer":
        """(Re)find the window. Needed after the target was closed and reopened."""
        from . import winutil

        window, error = winutil.resolve_window(self.spec, exclude_pid=self.exclude_pid)
        if window is None:
            raise WindowUnavailable(error)
        self.hwnd = window.hwnd
        self.title = window.title
        rect = window.rect
        self._last_rect = Region(rect[0], rect[1], rect[2], rect[3])
        return self

    @property
    def region(self) -> Region | None:
        """Current client area (after any sub-region), or the last known one."""
        from . import winutil

        if not winutil.is_alive(self.hwnd):
            return self._last_rect
        rect = winutil.client_rect(self.hwnd)
        if rect is None:
            return self._last_rect
        self._last_rect = self._apply_sub_region(
            Region(rect[0], rect[1], rect[2], rect[3])
        )
        return self._last_rect

    def describe(self) -> str:
        region = self.region
        where = f"{region}" if region else "unknown"
        fraction = ""
        if self.sub_region is not None:
            fraction = f" sub-region={self.sub_region}"
        return f"{self.title!r} hwnd={self.hwnd} client={where}{fraction}"

    # -- capture ----------------------------------------------------------- #

    def _sct(self) -> Any:
        sct = getattr(self._local, "sct", None)
        if sct is None:
            import mss

            sct = mss.MSS()
            self._local.sct = sct
        return sct

    def grab(self) -> np.ndarray:
        from . import winutil

        if not winutil.is_alive(self.hwnd):
            raise WindowUnavailable(f"window {self.hwnd} no longer exists")
        if winutil.is_minimized(self.hwnd):
            raise WindowUnavailable("window is minimised")
        rect = self.region
        if rect is None or not rect.valid:
            raise WindowUnavailable("window has no client area (hidden or zero sized)")

        shot = self._sct().grab(rect.as_mss())
        frame = np.asarray(shot, dtype=np.uint8)[:, :, :3]
        return np.ascontiguousarray(frame)

    def set_region(self, region: Region) -> None:
        """Not meaningful for a window: the window decides its own rectangle."""
        return None

    def set_window(self, spec: str) -> None:
        self.spec = spec
        self.resolve()

    def close(self) -> None:
        sct = getattr(self._local, "sct", None)
        if sct is not None:
            try:
                sct.close()
            except Exception:
                pass
            self._local.sct = None


class ChangeDetector:
    """Frame differencing that decides *when* OCR should run.

    Two different questions live here, and conflating them is what made scrolling
    text unrecognisable:

    **Has the frame changed?** A cheap grayscale signature compared against the
    previous frame. This is what the class used to answer, and it fires OCR on the
    very frame that changed -- which is exactly the wrong moment when the text is
    mid-animation, because the capture contains half-drawn, moving glyphs. For a
    subtitle that fades in over 200 ms, every frame in that window is garbage.

    **Has the frame stopped changing?** ``settle_s`` adds that second question.
    A change only re-arms the trigger; OCR is released once the frame has held
    still for at least that long. While text is still moving nothing is recognised,
    which is both more accurate and cheaper.

    ``settle_s = 0`` reproduces the old behaviour exactly (fire on the changing
    frame), so turning the gate on is opt-in and cannot regress the default.

    The clock is injectable so the gate can be asserted without sleeping, which is
    the difference between a test that runs in milliseconds and one that is flaky.
    """

    def __init__(
        self,
        threshold: float = 2.0,
        signature_width: int = 96,
        min_interval: float = 0.0,
        settle_s: float = 0.0,
    ) -> None:
        self.threshold = threshold
        self.signature_width = signature_width
        self.min_interval = min_interval
        #: How long the frame must hold still before OCR is released.
        self.settle_s = max(0.0, float(settle_s))
        self._previous: np.ndarray | None = None
        self._last_accepted = 0.0
        self._last_change_at: float | None = None
        #: A change happened and has not been recognised yet. Without this the
        #: trigger could not be edge-shaped: a settled frame would either fire
        #: every time or never.
        self._pending = False
        self.skipped = 0
        self.accepted = 0
        #: Frames where a change was waiting but the frame had not settled yet.
        #: This is the number that shows the gate doing work rather than idling.
        self.settle_waits = 0
        #: Most recent measured stability age, in seconds.
        self.stability_s = 0.0
        #: Most recent frame difference, for diagnostics.
        self.last_diff = 0.0

    def signature(self, frame: np.ndarray) -> np.ndarray:
        """Small grayscale fingerprint of a frame."""
        height, width = frame.shape[:2]
        if width == 0 or height == 0:
            return np.zeros((1, 1), dtype=np.float32)
        scale = self.signature_width / float(width)
        target = (max(1, self.signature_width), max(1, int(height * scale)))
        small = cv2.resize(frame, target, interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        return gray.astype(np.float32)

    def update(self, frame: np.ndarray, now: float | None = None) -> tuple[bool, float]:
        """Feed one frame; return ``(should_ocr, diff)``.

        ``should_ocr`` is edge shaped: it is true on the single frame where a
        change has settled, not on every frame thereafter.
        """
        if now is None:
            now = time.perf_counter()

        signature = self.signature(frame)
        previous = self._previous
        self._previous = signature

        if previous is None or previous.shape != signature.shape:
            diff = float("inf")
        else:
            diff = float(np.mean(np.abs(signature - previous)))
        self.last_diff = diff

        changed = diff >= self.threshold
        if changed:
            # Re-arm, and restart the settle timer from this frame.
            self._pending = True
            self._last_change_at = now

        if self._last_change_at is None:
            self.stability_s = 0.0
        else:
            self.stability_s = max(0.0, now - self._last_change_at)

        if not self._pending:
            self.skipped += 1
            return False, diff

        if self.settle_s > 0 and self.stability_s < self.settle_s:
            # A change is outstanding but the frame is still moving. Holding back
            # here is the whole point: recognising now would capture moving glyphs.
            self.settle_waits += 1
            self.skipped += 1
            return False, diff

        if self.min_interval > 0 and (now - self._last_accepted) < self.min_interval:
            self.skipped += 1
            return False, diff

        self._pending = False
        self._last_accepted = now
        self.accepted += 1
        return True, diff

    def armed(self) -> bool:
        """True when a change is waiting to be recognised."""
        return self._pending

    def reset(self) -> None:
        """Forget the previous frame and any outstanding change.

        Clearing the settle state matters as much as clearing the frame: a region
        change or a resume from pause invalidates the old "last changed at"
        timestamp, and keeping it would report a large stability age and release
        OCR on the strength of a frame that no longer exists.
        """
        self._previous = None
        self._pending = False
        self._last_change_at = None
        self._last_accepted = 0.0
        self.stability_s = 0.0
        self.last_diff = 0.0

