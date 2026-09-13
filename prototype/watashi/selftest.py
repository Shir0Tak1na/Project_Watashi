"""Self test: proves the pipeline works without needing a real screen.

This renders a synthetic subtitle image with PIL, runs it through the real OCR
engine and the real corpus/rule translator, and reports timings. It is the
headless verification harness for the prototype -- useful in CI, on a machine
with no display, and for checking a corpus or rule edit before pointing the
overlay at a live window.

Run with::

    run.cmd --selftest
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

#: Lines chosen to exercise every path: plain corpus hits, multi word corpus
#: hits, and words that are deliberately absent so the rule engine must act.
DEMO_LINES: tuple[str, ...] = (
    "the sword intent of this sect is a myth",
    "gg wp noob",
    "he broke through to the void realm",
    "antidragon superspirit voidsword",
)


def _load_font(size: int):
    from PIL import ImageFont

    for candidate in _FONT_CANDIDATES:
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
    font = _load_font(font_size)
    for index, line in enumerate(lines):
        y = margin + index * line_height
        draw.text((margin, y), line, font=font, fill=(foreground, foreground, foreground))

    rgb = np.asarray(image, dtype=np.uint8)
    return np.ascontiguousarray(rgb[:, :, ::-1])  # RGB -> BGR


@dataclass
class ModelProbe:
    """One line pushed through the local model, for the self test report."""

    source: str
    corpus_text: str
    model_text: str
    backend: str
    protected_terms: int
    elapsed_ms: float
    used_model: bool
    fallback_reason: str | None = None


@dataclass
class SelfTestReport:
    ocr_lines: list[str]
    ocr_confidence: float
    ocr_ms: float
    ocr_first_ms: float
    ocr_median_ms: float
    ocr_p95_ms: float
    ocr_repeats: int
    frame_shape: tuple[int, int]
    translate_ms: float
    coverage: float
    translated: str
    trace: str
    rule_probe: list[tuple[str, str, str, float]]
    change_detection: dict[str, object]
    corpus_entries: int
    rules: list[str]
    model_lines: list["ModelProbe"] = field(default_factory=list)
    model_summary: dict[str, object] = field(default_factory=dict)
    #: exceptions raised on the model path; non-zero makes the self test fail
    model_errors: int = 0

    def render(self) -> str:
        out: list[str] = []
        add = out.append
        add("=" * 72)
        add("Project Watashi - self test")
        add("=" * 72)
        add("")
        add(f"corpus entries : {self.corpus_entries}")
        add(f"rules loaded   : {', '.join(self.rules) if self.rules else '(none)'}")
        add("")
        add("-- synthetic frame OCR --")
        height, width = self.frame_shape
        add(f"frame size     : {width}x{height}")
        add(f"recognised {len(self.ocr_lines)} line(s), mean confidence {self.ocr_confidence:.2f}")
        for line in self.ocr_lines:
            add(f"    | {line}")
        add("")
        add("-- OCR latency (ONNX Runtime pays per input shape on first run) --")
        add(f"    first call     : {self.ocr_first_ms:8.1f} ms   <- includes shape/kernel init")
        add(f"    median of {self.ocr_repeats:<4d}: {self.ocr_median_ms:8.1f} ms   <- steady state")
        add(f"    p95            : {self.ocr_p95_ms:8.1f} ms")
        add(f"    R4 budget      : {'PASS' if self.ocr_median_ms <= 150.0 else 'OVER'} "
            f"(budget is 150 ms for OCR; this frame is {width}x{height})")
        add("")
        add("-- corpus + rules (the instant fast path) --")
        add(f"translated in {self.translate_ms:.1f} ms, coverage {self.coverage * 100:.0f}%")
        for line in self.translated.splitlines():
            add(f"    {line}")
        if self.trace:
            add("")
            add("-- provenance (span -> source of truth) --")
            for chunk in _wrap(self.trace, 68):
                add(f"    {chunk}")
        add("")
        add("-- rule engine probe (words absent from the corpus) --")
        for word, result, rule_id, confidence in self.rule_probe:
            marker = "OK " if rule_id else "-- "
            where = rule_id or "no rule matched (literal)"
            add(f"    {marker}{word:18s} -> {result:22s} [{where} {confidence:.2f}]")
        add("")
        if self.model_lines:
            add("-- local translation model (corpus terms protected) --")
            for probe in self.model_lines:
                add(f"    src     : {probe.source}")
                add(f"    corpus  : {probe.corpus_text}")
                if probe.used_model:
                    add(f"    model   : {probe.model_text}   ({probe.elapsed_ms:.0f} ms)")
                else:
                    add(f"    model   : (skipped) {probe.model_text}")
                    add(f"              reason: {probe.fallback_reason}")
                if probe.protected_terms:
                    add(f"              {probe.protected_terms} corpus term(s) carried through")
            add("")
            add("-- model summary --")
            for key, value in self.model_summary.items():
                add(f"    {key:34s} {value}")
        else:
            add("-- local translation model --")
            add("    not configured; run prototype/fetch_model.py to enable it")
        add("")
        add("-- change detection --")
        for key, value in self.change_detection.items():
            add(f"    {key:34s} {value}")
        add("")
        return "\n".join(out)


def _wrap(text: str, width: int) -> list[str]:
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def run_selftest(
    translator, ocr, target_lang: str = "zh-CN", repeats: int = 7, width: int = 1280
) -> SelfTestReport:
    """Run the whole local path against a synthetic frame.

    ``width`` should resemble the region a user would actually select. Region
    size is the dominant latency factor, so a self test on an oversized frame
    would report a budget failure that a realistic region would pass.
    """
    from .capture import ChangeDetector

    frame = render_text_image(DEMO_LINES, width=width)

    # First call trains ONNX Runtime for this input shape, so it is measured
    # separately from the steady state that the latency budget actually cares
    # about.
    started = time.perf_counter()
    ocr_result = ocr.recognize(frame)
    ocr_first_ms = (time.perf_counter() - started) * 1000.0

    samples: list[float] = []
    for _ in range(max(1, repeats)):
        started = time.perf_counter()
        ocr.recognize(frame)
        samples.append((time.perf_counter() - started) * 1000.0)
    samples.sort()
    median = samples[len(samples) // 2]
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]

    text = ocr_result.text
    started = time.perf_counter()
    outcome = translator.translate(text, target_lang) if text else None
    translate_ms = (time.perf_counter() - started) * 1000.0

    # probe the rule engine with words that are intentionally not in the corpus
    probe_words = ("antidragon", "superspirit", "voidsword", "warpdrive", "terraforming")
    probe: list[tuple[str, str, str, float]] = []
    for word in probe_words:
        result = translator.translate(word, target_lang)
        span = result.spans[0] if result.spans else None
        probe.append(
            (
                word,
                result.target_text,
                (span.rule_id or span.origin) if span else "",
                span.confidence if span else 0.0,
            )
        )

    # change detection: identical frame must be reported as unchanged
    detector = ChangeDetector(threshold=2.0)
    detector.update(frame)
    same_changed, same_diff = detector.update(frame.copy())
    other = render_text_image(("a completely different subtitle line",))
    other_changed, other_diff = detector.update(other)

    # The local model path, exercised explicitly. The hybrid translator serves
    # the corpus result instantly and refines asynchronously, so the self test
    # calls the refinement directly to prove that path works.
    model_lines: list[ModelProbe] = []
    model_summary: dict[str, object] = {}
    #: Counted separately from the model's own error tally so the self test can
    #: exit non-zero. It previously reported a broken model path as a pass,
    #: because the exception was caught per line and only printed.
    model_errors = 0
    model = getattr(translator, "model", None)
    if model is not None and getattr(model, "available", False):
        from .local_nmt import refine_with_nmt

        corpus_store = getattr(translator, "corpus", None)
        protect = getattr(translator, "protect", True)
        min_conf = getattr(translator, "protect_min_confidence", 0.4)
        source_lang = getattr(translator, "source_lang", None)
        for line in list(DEMO_LINES)[:3]:
            corpus_outcome = (
                corpus_store.translate(line, target_lang) if corpus_store else None
            )
            try:
                refinement = refine_with_nmt(
                    model, corpus_store, line, target_lang, protect, min_conf, source_lang
                )
                model_lines.append(
                    ModelProbe(
                        source=line,
                        corpus_text=corpus_outcome.target_text if corpus_outcome else "-",
                        model_text=refinement.target_text,
                        backend=refinement.backend,
                        protected_terms=refinement.protected_terms,
                        elapsed_ms=refinement.elapsed_ms,
                        used_model=refinement.used_nmt,
                        fallback_reason=refinement.fallback_reason,
                    )
                )
            except Exception as exc:
                model_errors += 1
                model_lines.append(
                    ModelProbe(
                        source=line,
                        corpus_text=corpus_outcome.target_text if corpus_outcome else "-",
                        model_text=f"ERROR {type(exc).__name__}: {exc}",
                        backend="error",
                        protected_terms=0,
                        elapsed_ms=0.0,
                        used_model=False,
                        fallback_reason=str(exc),
                    )
                )
        stats = model.stats
        model_summary = {
            "compute type": model.compute_type,
            "beam size": model.beam_size,
            "intra threads": model.intra_threads,
            "model load": f"{model.load_ms:.0f} ms",
            "sentences translated": stats.sentences,
            "mean latency": f"{stats.mean_ms:.0f} ms/sentence",
            "errors": stats.errors,
            "corpus terms protected": stats.protected_terms,
            "placeholders lost": stats.lost_placeholders,
            "harness errors": model_errors,
        }

    stats = translator.stats()
    return SelfTestReport(
        ocr_lines=[line.text for line in ocr_result.lines],
        ocr_confidence=ocr_result.mean_confidence,
        ocr_ms=ocr_first_ms,
        ocr_first_ms=ocr_first_ms,
        ocr_median_ms=median,
        ocr_p95_ms=p95,
        ocr_repeats=len(samples),
        frame_shape=(frame.shape[0], frame.shape[1]),
        translate_ms=translate_ms,
        coverage=outcome.coverage if outcome else 0.0,
        translated=outcome.target_text if outcome else "",
        trace=outcome.trace() if outcome else "",
        rule_probe=probe,
        change_detection={
            "identical frame -> changed": f"{same_changed}  (diff {same_diff:.3f})",
            "different frame -> changed": f"{other_changed}  (diff {other_diff:.3f})",
            "frames skipped as unchanged": detector.skipped,
            "frames sent to OCR": detector.accepted,
        },
        corpus_entries=int(stats.get("corpus_entries", 0)),
        rules=list(stats.get("rule_ids", [])),
        model_lines=model_lines,
        model_summary=model_summary,
        model_errors=model_errors,
    )
