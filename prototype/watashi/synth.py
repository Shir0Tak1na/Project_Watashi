"""Synthetic fixtures: rendered frames and a capturer that needs no screen.

These exist so the whole engine can be driven and asserted **headlessly** --
in CI, over SSH, or on a machine with no display. That matters more than it
sounds: the floating overlay cannot be tested automatically, but the pipeline,
the event stream and every command can be, as long as the frame source is
injectable.

``SyntheticCapturer`` deliberately changes its frame on a schedule, so change
detection, OCR, translation and refinement all actually run rather than being
skipped as "unchanged".
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Sequence

import numpy as np

from .capture import Region

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

#: Lines chosen to exercise every path: plain corpus hits, multi word corpus
#: hits, and words deliberately absent so the rule engine must act.
DEMO_LINES: tuple[str, ...] = (
    "the sword intent of this sect is a myth",
    "gg wp noob",
    "he broke through to the void realm",
    "antidragon superspirit voidsword",
)


def load_font(size: int):
    from PIL import ImageFont

    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_text_image(
    lines: Sequence[str],
    width: int = 1600,
    line_height: int = 64,
    font_size: int = 40,
    margin: int = 24,
    background: int = 0,
    foreground: int = 255,
) -> np.ndarray:
    """Render lines into a BGR image that looks like a subtitle area."""
    from PIL import Image, ImageDraw

    height = margin * 2 + line_height * len(lines)
    image = Image.new("RGB", (width, height), (background, background, background))
    draw = ImageDraw.Draw(image)
    font = load_font(font_size)
    for index, line in enumerate(lines):
        y = margin + index * line_height
        draw.text((margin, y), line, font=font, fill=(foreground, foreground, foreground))

    rgb = np.asarray(image, dtype=np.uint8)
    return np.ascontiguousarray(rgb[:, :, ::-1])  # RGB -> BGR


class SyntheticCapturer:
    """A capturer that renders text instead of reading the screen.

    Presents the same surface the pipeline needs (``grab``/``close``), plus a
    ``region`` attribute so region commands have something to report.
    """

    def __init__(
        self,
        frames: Sequence[Sequence[str]] | None = None,
        width: int = 1280,
        hold_seconds: float = 2.0,
    ) -> None:
        self.frames = list(frames) if frames else [
            (DEMO_LINES[0],),
            (DEMO_LINES[1],),
            (DEMO_LINES[2],),
            (DEMO_LINES[3],),
        ]
        self.width = width
        self.hold_seconds = hold_seconds
        self.closed = False
        self._index = 0
        self._switched_at = time.perf_counter()
        self._lock = threading.Lock()
        self._rendered: dict[int, np.ndarray] = {}
        # the real Region type, so region commands behave identically here
        self.region = Region(0, 0, width, 200)

    def _current_index(self) -> int:
        if self.hold_seconds > 0:
            now = time.perf_counter()
            if now - self._switched_at >= self.hold_seconds:
                with self._lock:
                    self._index = (self._index + 1) % len(self.frames)
                    self._switched_at = now
        return self._index

    def grab(self) -> np.ndarray:
        index = self._current_index()
        cached = self._rendered.get(index)
        if cached is None:
            cached = render_text_image(self.frames[index], width=self.width)
            self._rendered[index] = cached
        return cached.copy()

    def close(self) -> None:
        self.closed = True

    def set_region(self, region) -> None:
        """Accepted and recorded: there is no screen to crop, but the region
        command still needs to behave like the real thing."""
        self.region = region
