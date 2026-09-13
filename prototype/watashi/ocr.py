"""Local OCR via RapidOCR (ONNX Runtime).

Fully offline: the ONNX weights ship inside the ``rapidocr_onnxruntime``
package, so this satisfies requirement R1 -- no download, no cloud call.

Measured latency work
---------------------
RapidOCR's stock settings are tuned for full page documents, not for a wide
short subtitle strip, and they cost roughly **50x** latency here. Three
measured findings are applied on top of it (see ``bench_ocr.py`` and
``diagnose_ort.py`` to reproduce them):

1. ``Det.limit_type`` defaults to ``"min"`` with ``limit_side_len: 736``, which
   scales a 1280x176 strip up until its *short* side reaches 736 -- a **17x**
   pixel blowup (5344x736). Forcing ``"max"`` keeps the strip at native size.
2. ``enable_cpu_mem_arena`` is set to ``False`` by RapidOCR, so every inference
   reallocates its buffers instead of reusing a pool.
3. ``intra_op_num_threads`` is left at the ORT default (all cores). On a 20
   logical core machine that means ~20 threads fighting over a small model, and
   it is *slower* than 3-4 threads. Thread oversubscription costs ~5x here.

Together these take a 1280x176 strip from ~3580 ms to ~60-240 ms per frame.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import numpy as np


@dataclass
class OcrLine:
    """One recognised text line."""

    text: str
    confidence: float
    box: tuple[int, int, int, int]  # x1, y1, x2, y2 in region coordinates
    boxes: list[tuple[int, int, int, int]] = field(default_factory=list)

    @property
    def center_y(self) -> float:
        return (self.box[1] + self.box[3]) / 2.0

    @property
    def height(self) -> int:
        return max(1, self.box[3] - self.box[1])


@dataclass
class OcrResult:
    """Everything recognised in one frame."""

    lines: list[OcrLine]
    elapsed_ms: float
    scale: float = 1.0

    @property
    def text(self) -> str:
        """All lines joined, reading order, for subtitle display."""
        return "\n".join(line.text for line in self.lines).strip()

    @property
    def mean_confidence(self) -> float:
        if not self.lines:
            return 0.0
        return sum(l.confidence for l in self.lines) / len(self.lines)

    def __bool__(self) -> bool:
        return bool(self.lines)


def _box_to_xyxy(points: Sequence[Sequence[float]], scale: float) -> tuple[int, int, int, int]:
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    return (
        int(round(min(xs) / scale)),
        int(round(min(ys) / scale)),
        int(round(max(xs) / scale)),
        int(round(max(ys) / scale)),
    )


class RapidOcrEngine:
    """Thin, thread safe wrapper around RapidOCR.

    The defaults here are the measured ones, not the library's: see the module
    docstring for why each one matters.
    """

    def __init__(
        self,
        max_width: int = 640,
        use_cls: bool = False,
        use_det: bool = True,
        use_rec: bool = True,
        intra_op_threads: int = 4,
        inter_op_threads: int = 1,
        use_mem_arena: bool = True,
        det_limit_type: str = "max",
        det_limit_side_len: int | None = None,
        warmup: bool = True,
    ) -> None:
        self.max_width = max_width
        self.use_cls = use_cls
        self.use_det = use_det
        self.use_rec = use_rec
        self.intra_op_threads = intra_op_threads
        self.inter_op_threads = inter_op_threads
        self.use_mem_arena = use_mem_arena
        self.det_limit_type = det_limit_type
        self.det_limit_side_len = det_limit_side_len

        self._engine: Any = None
        self._lock = threading.Lock()
        self.load_ms: float = 0.0
        self.applied_settings: dict[str, Any] = {}
        self._kwargs = {
            "use_det": use_det,
            "use_cls": use_cls,
            "use_rec": use_rec,
        }
        self._supports_kwargs = True
        if warmup:
            self.ensure_loaded()

    # -- lifecycle -------------------------------------------------------- #

    def _session_options_builder(self) -> Callable[[dict], Any]:
        """Build a replacement for ``OrtInferSession._init_sess_opts``."""
        intra = self.intra_op_threads
        inter = self.inter_op_threads
        arena = self.use_mem_arena

        def build(config: dict) -> Any:
            from onnxruntime import GraphOptimizationLevel, SessionOptions

            options = SessionOptions()
            options.log_severity_level = 4
            # RapidOCR disables this; it forces a fresh allocation per run
            options.enable_cpu_mem_arena = arena
            options.graph_optimization_level = GraphOptimizationLevel.ORT_ENABLE_ALL
            if intra and intra > 0:
                options.intra_op_num_threads = intra
            if inter and inter > 0:
                options.inter_op_num_threads = inter
            return options

        return build

    def ensure_loaded(self) -> Any:
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is not None:
                return self._engine
            start = time.perf_counter()

            from rapidocr_onnxruntime import RapidOCR

            original = None
            patched = False
            try:
                from rapidocr_onnxruntime.utils import OrtInferSession

                original = OrtInferSession._init_sess_opts  # noqa: SLF001
                OrtInferSession._init_sess_opts = staticmethod(self._session_options_builder())  # noqa: SLF001
                patched = True
            except Exception as exc:
                print(f"[ocr] could not override ORT session options: {exc}")

            try:
                self._engine = RapidOCR()
            finally:
                if patched and original is not None:
                    from rapidocr_onnxruntime.utils import OrtInferSession

                    OrtInferSession._init_sess_opts = original  # noqa: SLF001

            self._apply_det_settings()
            self.load_ms = (time.perf_counter() - start) * 1000.0

            # warm up so the first real frame is not the slow one
            try:
                self._invoke(np.full((48, 320, 3), 255, dtype=np.uint8))
            except Exception:
                pass
            return self._engine

    def _apply_det_settings(self) -> None:
        """Stop the detector from upscaling wide, short subtitle strips."""
        detector = getattr(self._engine, "text_det", None)
        if detector is None:
            return
        applied: dict[str, Any] = {
            "intra_op_threads": self.intra_op_threads,
            "inter_op_threads": self.inter_op_threads,
            "enable_cpu_mem_arena": self.use_mem_arena,
        }
        if self.det_limit_type:
            before = getattr(detector, "limit_type", None)
            detector.limit_type = self.det_limit_type
            applied["det_limit_type"] = f"{before} -> {self.det_limit_type}"
        if self.det_limit_side_len is not None:
            detector.limit_side_len = self.det_limit_side_len
            applied["det_limit_side_len"] = self.det_limit_side_len
        self.applied_settings = applied

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    def _invoke(self, image: np.ndarray) -> Any:
        engine = self._engine
        if engine is None:
            raise RuntimeError("OCR engine not loaded")
        if self._supports_kwargs:
            try:
                return engine(image, **self._kwargs)
            except TypeError:
                # older/newer signature without these flags
                self._supports_kwargs = False
        return engine(image)

    # -- recognition ------------------------------------------------------ #

    def recognize(self, frame: np.ndarray) -> OcrResult:
        """Recognise text in a BGR frame."""
        start = time.perf_counter()
        if frame is None or frame.size == 0:
            return OcrResult(lines=[], elapsed_ms=0.0)

        image, scale = self._maybe_downscale(frame)

        with self._lock:
            self.ensure_loaded()
            try:
                raw = self._invoke(image)
            except Exception as exc:
                print(f"[ocr] recognition failed: {exc}")
                return OcrResult(lines=[], elapsed_ms=(time.perf_counter() - start) * 1000.0)

        boxes = self._unpack(raw)
        lines = self._group_lines(boxes, scale)
        elapsed = (time.perf_counter() - start) * 1000.0
        return OcrResult(lines=lines, elapsed_ms=elapsed, scale=scale)

    def _maybe_downscale(self, frame: np.ndarray) -> tuple[np.ndarray, float]:
        height, width = frame.shape[:2]
        if self.max_width <= 0 or width <= self.max_width:
            return frame, 1.0
        import cv2

        scale = self.max_width / float(width)
        resized = cv2.resize(
            frame,
            (self.max_width, max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        )
        return resized, scale

    @staticmethod
    def _unpack(raw: Any) -> list[tuple[list[list[float]], str, float]]:
        """Normalise the several shapes RapidOCR versions return."""
        if raw is None:
            return []
        result = raw
        if isinstance(raw, tuple):
            result = raw[0] if raw else None
        if result is None:
            return []
        out: list[tuple[list[list[float]], str, float]] = []
        for item in result:
            try:
                box, text, score = item[0], item[1], item[2]
            except (TypeError, IndexError):
                continue
            if text is None:
                continue
            out.append((box, str(text), float(score)))
        return out

    @staticmethod
    def _group_lines(
        boxes: Sequence[tuple[list[list[float]], str, float]], scale: float
    ) -> list[OcrLine]:
        """Merge boxes on the same visual line and sort into reading order."""
        if not boxes:
            return []

        items = []
        for raw_box, text, score in boxes:
            if not text.strip():
                continue
            xyxy = _box_to_xyxy(raw_box, scale)
            items.append((xyxy, text, score))
        if not items:
            return []

        heights = [b[3] - b[1] for b, _, _ in items]
        typical = float(np.median(heights)) if heights else 12.0
        tolerance = max(4.0, typical * 0.6)

        items.sort(key=lambda it: ((it[0][1] + it[0][3]) / 2.0, it[0][0]))

        lines: list[list[tuple[tuple[int, int, int, int], str, float]]] = []
        for item in items:
            center = (item[0][1] + item[0][3]) / 2.0
            placed = False
            for line in lines:
                ref = sum((b[1] + b[3]) / 2.0 for b, _, _ in line) / len(line)
                if abs(center - ref) <= tolerance:
                    line.append(item)
                    placed = True
                    break
            if not placed:
                lines.append([item])

        out: list[OcrLine] = []
        for line in lines:
            line.sort(key=lambda it: it[0][0])
            # join with a space for latin runs, nothing for CJK
            parts: list[str] = []
            for index, (_, text, _) in enumerate(line):
                if index and _needs_space(parts[-1], text):
                    parts.append(" ")
                parts.append(text)
            text = "".join(parts).strip()
            if not text:
                continue
            x1 = min(b[0] for b, _, _ in line)
            y1 = min(b[1] for b, _, _ in line)
            x2 = max(b[2] for b, _, _ in line)
            y2 = max(b[3] for b, _, _ in line)
            confidence = sum(s for _, _, s in line) / len(line)
            out.append(
                OcrLine(
                    text=text,
                    confidence=confidence,
                    box=(x1, y1, x2, y2),
                    boxes=[b for b, _, _ in line],
                )
            )

        out.sort(key=lambda l: (l.box[1], l.box[0]))
        return out


_CJK_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0x3040, 0x30FF),
    (0xAC00, 0xD7AF),
    (0xFF00, 0xFFEF),
)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def _needs_space(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return not (_is_cjk(left[-1]) or _is_cjk(right[0]))
