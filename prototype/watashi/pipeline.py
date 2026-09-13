"""The real time pipeline: capture -> change detect -> OCR -> translate -> overlay.

Threading model (this is what keeps latency low, requirement R4):

::

    [capture thread]  grab region, diff against previous frame
             |
             |  newest frame only, stale frames are overwritten
             v
    [worker thread]   OCR -> corpus/rules -> publish to overlay queue
             |
             v
    [Tk main thread]  drain queue, redraw (16 ms tick)

The critical detail is the **latest frame slot**. If OCR is slower than the
capture rate, the capture thread does not queue frames up -- it overwrites the
single pending slot. Stale frames therefore never delay the newest one, so the
subtitle always reflects "now" instead of drifting further and further behind.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .capture import ChangeDetector, RegionCapturer
from .events import OverlayStats, OverlayUpdate, TranslatedLine
from .lang import has_translatable_content, matches_target
from .recent import RecentTranslations
from .ocr import OcrResult, RapidOcrEngine
from .translate import Context, Translator


class _LatestSlot:
    """A single item mailbox that always holds the newest value."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._item: np.ndarray | None = None
        self._closed = False
        self.overwritten = 0

    def put(self, item: np.ndarray) -> None:
        with self._condition:
            if self._item is not None:
                self.overwritten += 1
            self._item = item
            self._condition.notify()

    def get(self) -> np.ndarray | None:
        with self._condition:
            while self._item is None and not self._closed:
                self._condition.wait(timeout=0.2)
            if self._closed and self._item is None:
                return None
            item, self._item = self._item, None
            return item

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def clear(self) -> int:
        """Drop the pending frame, if any. Returns how many were discarded."""
        with self._condition:
            dropped = 1 if self._item is not None else 0
            self._item = None
            return dropped


@dataclass
class PipelineConfig:
    fps: float = 10.0
    diff_threshold: float = 2.0
    signature_width: int = 96
    min_ocr_interval: float = 0.0
    #: Hold OCR back until the frame has stopped changing for this many seconds.
    #: 0 = recognise on the changing frame, which is the original behaviour.
    #: A non-zero value is what makes scrolling/animated text usable: recognising
    #: mid-motion captures half-drawn glyphs.
    settle_s: float = 0.0
    #: Reuse a translation seen within this many seconds instead of paying for it
    #: again. 0 disables the memory entirely.
    dedup_ttl_s: float = 10.0
    #: Turn the reuse off without losing the counters.
    dedup: bool = True
    #: Recognise everything, but only translate and draw the largest N boxes. 0 = no
    #: cap. See Pipeline._cap_boxes for what this does and does not save.
    max_boxes: int = 0
    max_width: int = 1280
    target_lang: str = "zh-CN"
    source_lang: str = "auto"
    min_confidence: float = 0.0
    show_source_in_bar: bool = True
    #: The scene/domain the user selected, e.g. "finance". An empty string means no
    #: preference, which is the default and makes the dimension inert: every entry is
    #: then equally eligible and the engine answers exactly as it did before scenes
    #: existed. Compared against ``Entry.domain`` when a term has several senses.
    scene: str = ""


class Pipeline:
    """Runs capture and OCR workers and feeds the overlay."""

    def __init__(
        self,
        capturer: RegionCapturer,
        ocr: RapidOcrEngine,
        translator: Translator,
        config: PipelineConfig | None = None,
        on_update: Callable[[OverlayUpdate], None] | None = None,
        on_stats: Callable[[OverlayStats], None] | None = None,
    ) -> None:
        self.capturer = capturer
        self.ocr = ocr
        self.translator = translator
        self.config = config or PipelineConfig()
        self.on_update = on_update
        self.on_stats = on_stats

        self.detector = ChangeDetector(            threshold=self.config.diff_threshold,
            signature_width=self.config.signature_width,
            min_interval=self.config.min_ocr_interval,
            settle_s=self.config.settle_s,
        )
        self._slot = _LatestSlot()
        self._stop = threading.Event()
        self._paused = threading.Event()
        #: Set while OCR work is pending or in flight. The local model worker
        #: waits on this before refining: OCR and NMT inference contend for
        #: memory bandwidth, and running them together triples OCR latency
        #: (measured 73 ms -> 266 ms). OCR wins because it feeds the screen.
        self.ocr_busy = threading.Event()
        self._threads: list[threading.Thread] = []
        #: Short-term memory of what was just translated. OCR is not deterministic, so
        #: the same sentence returns with a different trailing punctuation or space and
        #: each variant misses the translation cache; this is what stops the model
        #: being paid again for text it has already read.
        self.recent = RecentTranslations(
            ttl_s=self.config.dedup_ttl_s, enabled=self.config.dedup
        )
        self.reused_lines = 0
        #: lines where the translator returned the source unchanged, i.e. nothing
        #: translated them. Visible so a language pair with no coverage shows up as a
        #: number instead of as a screen full of untranslated text.
        self.untranslated_lines = 0
        #: boxes dropped by the cap, so a limit that is biting is visible rather than
        #: silently changing what the user sees
        self.capped_lines = 0
        self._cap_active = False

        self._lock = threading.Lock()
        self._total_ms: deque[float] = deque(maxlen=60)
        self._ocr_ms: deque[float] = deque(maxlen=60)
        self._translate_ms: deque[float] = deque(maxlen=60)
        self._frame_times: deque[float] = deque(maxlen=60)
        self._frames = 0
        self._errors = 0
        #: frames where every line was already the target language, so no
        #: translation or refinement was needed at all
        self.passthrough_frames = 0
        #: lines skipped because they were already the target language
        self.passthrough_lines = 0
        #: lines skipped because they hold no letters at all (clock, symbols)
        self.nontranslatable_lines = 0
        self._last_result: OcrResult | None = None

    # -- control ---------------------------------------------------------- #

    def start(self, warmup: bool = True) -> None:
        if warmup:
            # pay the model load cost up front instead of on the first frame
            self.ocr.ensure_loaded()
        # let the model worker know when to stay out of OCR's way
        setattr(self.translator, "defer_while", self.ocr_busy.is_set)
        self._stop.clear()
        capture = threading.Thread(target=self._capture_loop, name="watashi-capture", daemon=True)
        worker = threading.Thread(target=self._work_loop, name="watashi-ocr", daemon=True)
        self._threads = [capture, worker]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._slot.close()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []
        self.capturer.close()

    def _already_target(self, text: str, declared_source: str) -> bool:
        """Should this line be passed through untranslated?

        Three branches, in order of how much the evidence is worth:

        1. **A decisive script verdict wins.** Han, kana, hangul, Thai, Cyrillic and
           Arabic identify a language on their own, so if the text is written in the
           target's script it is already the target language -- whatever the source
           was declared to be. Skipping this branch is what made a declared
           ``--source en`` translate Chinese text on every single frame.
        2. **Otherwise trust the declaration.** A Latin-script verdict is only "there
           were letters", which cannot tell French from English, so when the user
           says the source is French we believe them rather than the guess.
        3. **Otherwise fall back to the script guess**, which is all that is available
           when nothing was declared. This is the branch that passes undeclared
           French through as English; the fix is to declare the source, and the
           limitation is documented rather than hidden.
        """
        from .lang import decisive_language, same_language

        decisive = decisive_language(text)
        if decisive is not None:
            return same_language(decisive, self.config.target_lang)
        if declared_source and declared_source != "auto":
            try:
                return same_language(declared_source, self.config.target_lang)
            except ValueError:
                return False
        return matches_target(text, self.config.target_lang)

    def _cap_boxes(self, result: Any) -> Any:
        """Keep only the largest N recognised boxes, when a cap is configured.

        What this saves, stated honestly because the obvious reading is wrong: OCR
        detects and recognises in a single call, so a cap applied *after* that call
        cannot save recognition time -- the work is already done. What it does save is
        everything downstream of recognition, and that is the expensive part here:
        measured, one box costs about 20 ms to recognise but a sentence costs 71-350 ms
        to translate with the local model. On a dense screen, refusing to translate 40
        of 50 boxes is a much larger saving than the recognition it cannot avoid, and it
        keeps the overlay and the history readable instead of flooded.

        The largest boxes win because on a real screen the small ones are
        disproportionately noise: a fragment of a texture or a UI ornament picked up as
        a single character. Dropping them improves what is shown as well as how much.

        Reading order is restored afterwards, so a cap never reorders the display.
        """
        limit = int(self.config.max_boxes or 0)
        if limit <= 0:
            return result
        lines = list(getattr(result, "lines", []) or [])
        if len(lines) <= limit:
            return result

        def area(line: Any) -> int:
            box = getattr(line, "box", None)
            if not box:
                return 0
            return int(box[2]) * int(box[3])

        kept = sorted(lines, key=area, reverse=True)[:limit]
        kept.sort(key=lambda line: (getattr(line, "box", (0, 0, 0, 0)) or (0, 0, 0, 0))[1])
        self.capped_lines += len(lines) - len(kept)
        self._cap_active = True
        try:
            result.lines = kept
        except AttributeError:
            return result
        return result

    def set_capturer(self, capturer: Any, close_old: bool = True) -> None:
        """Swap the capture source while the pipeline is running.

        Safe because the capture loop reads ``self.capturer`` on every iteration
        rather than holding a local reference, so a single attribute assignment is
        the switch. The detector is reset because the new source has a different
        geometry and the old frame signature would compare two unrelated images --
        which would either suppress the first real frame or fire on it spuriously.
        """
        old = self.capturer
        self.capturer = capturer
        self.detector.reset()
        self._slot.clear()
        if close_old and old is not None and old is not capturer:
            try:
                old.close()
            except Exception:
                pass

    def pause(self) -> None:
        """Stop producing results immediately.

        Dropping the pending frame matters: without it a frame already queued
        would still be OCR'd and published, so a user who hits pause would see
        one more subtitle appear. Pause should be deterministic.

        Announcing it matters just as much. Stats are emitted at the end of a
        processed frame, and a paused pipeline processes none -- so without this the
        last stats event stays ``paused: False`` forever, and every surface that draws
        its state from stats keeps saying "recognising". That is exactly what the user
        reported: pressing pause changed nothing they could see.
        """
        self._paused.set()
        self._slot.clear()
        self._emit_stats()

    def resume(self) -> None:
        self._paused.clear()
        self.detector.reset()
        self._emit_stats()

    def toggle_pause(self) -> bool:
        if self._paused.is_set():
            self.resume()
            return False
        self.pause()
        return True

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    # -- loops ------------------------------------------------------------ #

    def _capture_loop(self) -> None:
        period = 1.0 / self.config.fps if self.config.fps > 0 else 0.0
        next_at = time.perf_counter()
        while not self._stop.is_set():
            if self._paused.is_set():
                time.sleep(0.05)
                next_at = time.perf_counter()
                continue
            try:
                frame = self.capturer.grab()
            except Exception as exc:
                self._errors += 1
                print(f"[pipeline] capture failed: {exc}")
                time.sleep(0.25)
                continue

            changed, _diff = self.detector.update(frame)
            if changed:
                self.ocr_busy.set()
                self._slot.put(frame)

            if period > 0:
                next_at += period
                delay = next_at - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                else:
                    # we are behind; resynchronise instead of accumulating debt
                    next_at = time.perf_counter()

    def _work_loop(self) -> None:
        while not self._stop.is_set():
            frame = self._slot.get()
            if frame is None:
                if self._stop.is_set():
                    return
                continue

            started = time.perf_counter()
            try:
                ocr_result = self.ocr.recognize(frame)
            except Exception as exc:
                self._errors += 1
                print(f"[pipeline] OCR failed: {exc}")
                continue

            ocr_result = self._cap_boxes(ocr_result)

            if self._paused.is_set():
                # pause landed while this frame was in flight; honour it rather
                # than publishing one more subtitle after the fact
                self.ocr_busy.clear()
                continue

            try:
                update = self._make_update(ocr_result, started)
            except Exception as exc:
                self._errors += 1
                print(f"[pipeline] translate failed: {exc}")
                continue

            elapsed_ms = (time.perf_counter() - started) * 1000.0
            with self._lock:
                self._frames += 1
                self._frame_times.append(time.perf_counter())
                self._total_ms.append(elapsed_ms)
                self._ocr_ms.append(ocr_result.elapsed_ms)
                self._translate_ms.append(max(0.0, elapsed_ms - ocr_result.elapsed_ms))
            self._last_result = ocr_result

            if update is not None:
                if self.on_update is not None:
                    self.on_update(update)

            # OCR work is done for this frame; let the model take the CPU
            self.ocr_busy.clear()
            self._emit_stats()

    # -- translation ------------------------------------------------------ #

    def _context(self) -> Context:
        """What this frame's lookups are allowed to depend on.

        Built once per frame rather than per line, and it is the *same object* the
        caches key on, so a scene change or a window change cannot be honoured by the
        lookup and ignored by the cache (or the other way round).

        The window title is read from the capturer, which is the only place that knows
        it: a window-following capture has one, a fixed region does not. An entry that
        requires a window is then simply not applied to text that came from a region,
        rather than being applied everywhere. Read per frame rather than remembered, so
        a capture that re-resolves its window is described by its current title.
        """
        return Context(
            target_lang=self.config.target_lang,
            scene=self.config.scene,
            window=self._window_title(),
        )

    def _window_title(self) -> str:
        """The captured window's title, or "" when the source has no window."""
        for name in ("window_title", "title"):
            value = getattr(self.capturer, name, None)
            if callable(value):
                try:
                    value = value()
                except Exception:
                    continue
            if isinstance(value, str):
                return value
        return ""

    def _context_scope(self, context: Context) -> tuple[str, ...]:
        """The cache-key scope for this frame, from the corpus when it can say.

        Asks the corpus because it is the corpus that knows what it can distinguish: a
        vocabulary with no scene tags and no window conditions yields the target
        language alone, so the caches behave exactly as they did before either existed
        and do not fragment into misses for dimensions that cannot change an answer.
        """
        asker = getattr(self.translator, "context_key", None)
        if callable(asker):
            try:
                return tuple(asker(context))
            except Exception:
                pass
        corpus = getattr(self.translator, "corpus", None)
        asker = getattr(corpus, "context_key", None)
        if callable(asker):
            try:
                return tuple(asker(context))
            except Exception:
                pass
        return (context.target_lang,)

    def _make_update(self, ocr_result: OcrResult, started: float) -> OverlayUpdate | None:
        """Translate each recognised line once, keeping its geometry.

        Translating per line rather than per frame means the result carries the
        ``lines`` array with each line's box, which is what layouts such as the
        in-place overlay need. It also avoids the previous duplication where the
        frame was translated as a whole *and* line by line.

        Lines already written in the target language are passed through
        untouched. Without that check, Chinese on screen is "translated" into
        Chinese: the user sees their own language rewritten, the local model
        spends ~300-600 ms per line rephrasing text that needed no work, and the
        subtitle keeps changing for no reason.
        """
        if not ocr_result.lines:
            return None

        translated: list[TranslatedLine] = []
        passthrough: list[bool] = []
        traces: list[str] = []
        backends: set[str] = set()
        declared_source = (self.config.source_lang or "auto").strip().lower()
        context = self._context()
        # What the caches may be keyed on: exactly the parts of this frame's context that
        # the loaded vocabulary can tell apart. Computed once per frame, not per line.
        scope = self._context_scope(context)
        for ocr_line in ocr_result.lines:
            if not ocr_line.text.strip():
                continue

            if not has_translatable_content(ocr_line.text):
                # Digits, punctuation, symbols: a clock, a countdown, a divider. They
                # are kept on screen unchanged because that is what they look like,
                # but they must never reach the translator -- each tick is a fresh
                # "sentence" that misses the cache, so the model would run forever on
                # text that has no language in it.
                translated.append(
                    TranslatedLine(
                        source=ocr_line.text,
                        target=ocr_line.text,
                        box=ocr_line.box,
                        confidence=1.0,
                        coverage=1.0,
                    )
                )
                passthrough.append(True)
                traces.append("skip: no letters to translate")
                backends.add("skip")
                self.nontranslatable_lines += 1
                continue

            # Already translated a moment ago? Reuse it rather than paying the model
            # again. The box comes from *this* frame, not from the remembered one, so
            # the plate still follows text that has moved -- suppressing the cost and
            # suppressing the position are different things and only the first is
            # wanted here.
            remembered = self.recent.get(ocr_line.text, scope=scope)
            if remembered is not None:
                translated.append(
                    TranslatedLine(
                        source=ocr_line.text,
                        target=remembered,
                        box=ocr_line.box,
                        confidence=ocr_line.confidence,
                        coverage=1.0,
                    )
                )
                passthrough.append(True)
                traces.append("reuse: identical to a line translated a moment ago")
                backends.add("reuse")
                self.reused_lines += 1
                continue

            if self._already_target(ocr_line.text, declared_source):
                translated.append(
                    TranslatedLine(
                        source=ocr_line.text,
                        target=ocr_line.text,
                        box=ocr_line.box,
                        confidence=1.0,
                        coverage=1.0,
                    )
                )
                passthrough.append(True)
                traces.append("passthrough: already the target language")
                backends.add("passthrough")
                continue

            outcome = self.translator.translate(
                ocr_line.text, self.config.target_lang, context
            )
            target = outcome.target_text or ocr_line.text
            # An echo is not a translation, and presenting it as one is worse than
            # showing nothing: the user reads the original back and concludes the
            # translation is broken. It happens whenever the corpus has no coverage for
            # the language pair -- the shipped corpus and rules are en<->zh only, so
            # zh->ja comes back as the Chinese it went in as -- and the transliterate
            # fallback is what returns it.
            #
            # Reported as zero coverage rather than dropped, so the line still appears
            # (the text is on screen either way) but is drawn as the uncertain result it
            # is, and the model's refinement can replace it a moment later.
            echoed = _normalize_for_echo(target) == _normalize_for_echo(ocr_line.text)
            coverage = 0.0 if echoed else outcome.coverage
            translated.append(
                TranslatedLine(
                    source=ocr_line.text,
                    target=target,
                    box=ocr_line.box,
                    confidence=outcome.confidence,
                    coverage=coverage,
                )
            )
            passthrough.append(False)
            traces.append(
                "untranslated: no coverage for this language pair, the source is "
                "unchanged" if echoed else outcome.trace()
            )
            if echoed:
                backends.add("untranslated")
                self.untranslated_lines += 1
            elif outcome.backend:
                backends.add(outcome.backend)
            # Remember it so the next frame that reads the same sentence -- which OCR
            # will render slightly differently -- does not pay for it again.
            self.recent.put(ocr_line.text, target, scope=scope)

        if not translated:
            return None

        passthrough_count = sum(1 for flag in passthrough if flag)
        self.passthrough_lines += passthrough_count

        source_text = "\n".join(line.source for line in translated)
        target_text = "\n".join(line.target for line in translated)
        mean_confidence = sum(line.confidence for line in translated) / len(translated)
        if mean_confidence < self.config.min_confidence:
            return None

        # Refine the lines that actually needed translating, as a batch. Lines
        # already in the target language are excluded so the model is not asked
        # to rewrite text that needed no work.
        needs_refinement = [
            line.source
            for line, was_passthrough in zip(translated, passthrough)
            if not was_passthrough
        ]
        if needs_refinement:
            submit = getattr(self.translator, "submit", None)
            if callable(submit):
                try:
                    submit(needs_refinement, self.config.target_lang, context)
                except Exception as exc:
                    self._errors += 1
                    print(f"[pipeline] could not queue refinement: {exc}")
        else:
            self.passthrough_frames += 1

        return OverlayUpdate(
            source_text=source_text,
            target_text=target_text,
            coverage=sum(line.coverage for line in translated) / len(translated),
            confidence=mean_confidence,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            trace="\n".join(t for t in traces if t),
            backend=", ".join(sorted(backends)),
            lines=translated,
        )

    # -- stats ------------------------------------------------------------ #

    def _emit_stats(self) -> None:
        if self.on_stats is None:
            return
        stats = self.stats()
        self.on_stats(stats)

    def stats(self) -> OverlayStats:
        with self._lock:
            total = _mean(self._total_ms)
            ocr_ms = _mean(self._ocr_ms)
            translate_ms = _mean(self._translate_ms)
            fps = _fps(self._frame_times)
            frames = self._frames
            skipped = self.detector.skipped
        backend_stats = self.translator.stats()
        refinements = int(backend_stats.get("refinements", 0))
        # "pending" is a job waiting to run; "dropped" counts requests superseded
        # before they ran. These are different numbers and were once conflated,
        # which made one figure appear twice under two names.
        pending = int(backend_stats.get("refinements_pending", 0))
        dropped = int(backend_stats.get("refinements_dropped", 0))
        return OverlayStats(
            fps=fps,
            ocr_ms=ocr_ms,
            translate_ms=translate_ms,
            total_ms=total,
            frames=frames,
            skipped=skipped,
            corpus_entries=int(backend_stats.get("corpus_entries", 0)),
            rules=int(backend_stats.get("rules", 0)),
            cache_hit_rate=float(backend_stats.get("cache_hit_rate", 0.0)),
            backend=str(backend_stats.get("backend", "")),
            paused=self.paused,
            refinements=refinements,
            refinements_pending=pending,
            refinements_dropped=dropped,
        )

    def to_dict(self) -> dict:
        s = self.stats()
        slot = self._slot
        return {
            "fps": round(s.fps, 2),
            "ocr_ms": round(s.ocr_ms, 2),
            "translate_ms": round(s.translate_ms, 2),
            "total_ms": round(s.total_ms, 2),
            "frames": s.frames,
            "frames_skipped_unchanged": s.skipped,
            "frames_overwritten": slot.overwritten,
            "errors": self._errors,
            "corpus_entries": s.corpus_entries,
            "rules": s.rules,
            "cache_hit_rate": round(s.cache_hit_rate, 3),
            "backend": s.backend,
            "refinements": s.refinements,
            "refinements_pending": s.refinements_pending,
            "refinements_dropped": s.refinements_dropped,
            "passthrough_frames": self.passthrough_frames,
            "passthrough_lines": self.passthrough_lines,
            #: letterless lines (clock, symbols) kept on screen but never translated
            "nontranslatable_lines": self.nontranslatable_lines,
            # --- the stability gate ------------------------------------------- #
            # `settle_s` is the requested threshold; `settle_waits` is how many
            # frames were deliberately held back because the frame was still
            # moving. Without that counter the gate could be doing nothing at all
            # and the latency numbers would look identical.
            "settle_s": round(self.config.settle_s, 3),
            "settle_waits": self.detector.settle_waits,
            "stability_s": round(self.detector.stability_s, 3),
            "last_diff": round(self.detector.last_diff, 3),
            "detector_accepted": self.detector.accepted,
            # --- text reuse ---------------------------------------------------- #
            # `reused_lines` is the number that matters: it is how many times the
            # model was NOT paid for text already translated. Without it a memory
            # that never matches looks exactly like one that works.
            "reused_lines": self.reused_lines,
            #: how many lines came back unchanged because nothing could translate them
            "untranslated_lines": self.untranslated_lines,
            #: how many recognised boxes the cap dropped before translating them
            "capped_lines": self.capped_lines,
            "max_boxes": int(self.config.max_boxes or 0),
            **self.recent.stats(),
            "refinements_deferred": int(
                self.translator.stats().get("refinements_deferred", 0)
            ),
            "ocr_load_ms": round(self.ocr.load_ms, 1),
            "nmt": {
                key: value
                for key, value in self.translator.stats().items()
                if key.startswith("nmt_")
            },
            "last_lines": [l.text for l in (self._last_result.lines if self._last_result else [])],
        }


def _normalize_for_echo(text: str) -> str:
    """Fold the differences OCR introduces, so an echo is recognised as one.

    Reuses the reuse layer's normalisation on purpose: both are asking "is this the
    same string?", and two different answers to that question would be a bug waiting
    to happen.
    """
    from .recent import normalize

    return normalize(text)


def _mean(values: deque[float]) -> float:
    return float(sum(values) / len(values)) if values else 0.0


def _fps(times: deque[float]) -> float:
    if len(times) < 2:
        return 0.0
    span = times[-1] - times[0]
    if span <= 0:
        return 0.0
    return (len(times) - 1) / span
