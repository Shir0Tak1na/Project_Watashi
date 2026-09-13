"""Session: the engine facade that every UI talks to.

This is the boundary the whole UI architecture rests on. A ``Session`` owns the
config, the corpus, the translator, the OCR engine, the capturer and the
pipeline, and exposes exactly three things:

* **events** -- a fan-out stream of ``events.py`` envelopes (subtitle,
  refinement, line, stats, status, error)
* **commands** -- ``pause``, ``set_region``, ``reload_corpus``, ...
* **introspection** -- ``stats()``, ``info()``, ``describe()``

Surfaces are adapters over that surface and nothing else:

===========================  ==================================================
CLI / PowerShell             drains events, prints JSON lines or text
floating overlay             ``subscribe()`` in-process queue, no serialisation
local web panel              same events over SSE, commands over POST
future LAN client            the identical schema over a socket
===========================  ==================================================

Because every client shares one schema, adding a surface is an adapter, not a
second copy of the application. Nothing here imports tkinter or a web framework:
``--no-ui``, ``--serve`` and ``--mode both`` all run the same object.

The capturer is injectable so the entire engine can be driven headlessly
(``SyntheticCapturer``) and asserted in CI -- the one thing that cannot be
automated is the overlay window itself.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import RELEASE_STAGE, __version__
from .capture import Region, RegionCapturer, list_monitors
from .config import AppConfig
from .events import (
    CMD_CORRECT,
    CMD_EXPORT,
    CMD_LIBRARY_DELETE,
    CMD_LIBRARY_EXPORT,
    CMD_LIBRARY_IMPORT,
    CMD_LIBRARY_LIST,
    CMD_LIBRARY_PUT,
    CMD_LIBRARY_RESTORE,
    CMD_LIBRARY_SUPPRESS,
    CMD_LIST_CORRECTIONS,
    CMD_PAUSE,
    CMD_LOAD_PROFILE,
    CMD_RELOAD_CORPUS,
    CMD_REMOVE_CORRECTION,
    CMD_RESUME,
    CMD_SET_CORPUS_RELOAD,
    CMD_SET_DIFF_THRESHOLD,
    CMD_SET_FPS,
    CMD_SET_SELF_CAPTURE,
    CMD_SET_PRESENTATION,
    CMD_SET_REGION,
    CMD_SET_TARGET_LANG,
    CMD_SHUTDOWN,
    CMD_STATUS,
    CMD_TOGGLE_PAUSE,
    CMD_USE_WINDOW,
    EVENT_CORRECTION,
    EVENT_ERROR,
    EVENT_LIBRARY,
    EVENT_PRESENTATION,
    EVENT_READY,
    EVENT_REFINEMENT,
    EVENT_SETTINGS,
    EVENT_STATS,
    EVENT_STATUS,
    EVENT_STOPPED,
    EVENT_SUBTITLE,
    SCHEMA_VERSION,
    OverlayStats,
    OverlayUpdate,
    command_result,
    encode_event,
    encode_refinement,
    encode_stats,
    encode_update,
)
from .memory import MemorySampler
from .presentation import PresentationSpec, resolve_presentation
from .profiles import list_profiles
from .ocr import RapidOcrEngine
from .pipeline import Pipeline, PipelineConfig
from .plugins import PluginRegistry, plugin_directories
from . import correct as corrections
from . import library as library_module
from .translate import CorpusStore, LAYER_DOMAIN, LAYER_GENERAL, LAYER_USER, Translator

#: How many pending events a slow subscriber may accumulate before old display
#: events are dropped. Counters and commands are never dropped.
_SUBSCRIBER_LIMIT = 256


class Session:
    """Owns the engine and publishes a transport agnostic event stream."""

    def __init__(
        self,
        config: AppConfig,
        *,
        capturer: Any | None = None,
        ocr: RapidOcrEngine | None = None,
        translator: Translator | None = None,
        memory_sampler: MemorySampler | None = None,
        presentation: Any | None = None,
        plugins: Any | None = None,
    ) -> None:
        self.config = config
        self._capturer = capturer
        self._ocr = ocr
        self._translator = translator
        self._memory = memory_sampler or MemorySampler()
        self._presentation = presentation or resolve_presentation(config)
        #: extension points registered from user code; see watashi/plugins.py
        self.plugins = plugins if plugins is not None else PluginRegistry()
        self.plugins_loaded = False
        self._postprocess_enabled = bool(config.plugins.get("postprocess", True))
        #: recognised frames, for the export extension point. Bounded, because an
        #: always-on overlay would otherwise grow without limit.
        limit = int(config.plugins.get("history_limit", 2000) or 2000)
        self.history: deque[dict[str, Any]] = deque(maxlen=max(1, limit))
        self._last_export: str | None = None
        #: the edited corpus file, created on first use by the corpus editor
        self._library: Any | None = None
        #: the frame currently on screen, so a correction can repaint it without
        #: waiting for OCR to read the same text again (which it may never do)
        self._last_update: OverlayUpdate | None = None

        self._pipeline: Pipeline | None = None
        self._subscribers: list[queue.Queue] = []
        self._subscribers_lock = threading.Lock()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._running = False
        self._started_at = 0.0
        self._last_error: str | None = None
        self._status = ""
        self._region = config.region
        #: Surfaces wanting live presentation changes register here. The session
        #: cannot import the overlay (that would drag tkinter into the engine),
        #: so the CLI wires the sink up.
        self.presentation_sinks: list[Callable[[Any], None]] = []

    @property
    def presentation(self) -> Any:
        return self._presentation

    def on_presentation_change(self, callback: Callable[[Any], None]) -> None:
        self.presentation_sinks.append(callback)

    def _publish_presentation(self) -> None:
        spec = self._presentation
        for sink in list(self.presentation_sinks):
            try:
                sink(spec)
            except Exception as exc:
                print(f"[session] presentation sink failed: {exc}")
        self.publish(EVENT_PRESENTATION, spec.to_dict(), droppable=False)

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #

    def build(self) -> None:
        """Create translator, OCR engine, capturer and pipeline.

        Model loading happens here so its cost is paid once, up front, and so a
        failure is reported as an event instead of mid-frame.
        """
        self.load_plugins()
        if self._translator is None:
            self._translator = build_translator(self.config, on_refined=self._on_refined)
        if self._ocr is None:
            self._ocr = build_ocr(self.config)
        if self._capturer is None:
            self._capturer = RegionCapturer(
                region=self.config.region,
                monitor=self.config.monitor,
                # the configured strip height now reaches the capturer instead of being
                # stored and ignored
                height_ratio=self.config.capture.get("region_ratio"),
            )

        pipeline_config = PipelineConfig(
            fps=self.config.fps,
            diff_threshold=float(self.config.capture.get("diff_threshold", 2.0)),
            signature_width=int(self.config.capture.get("signature_width", 96)),
            min_ocr_interval=float(self.config.capture.get("min_ocr_interval", 0.0)),
            #: 0 = recognise on the changing frame (original behaviour). Non-zero
            #: holds OCR back until the frame has been still that long, which is
            #: what makes animated or scrolling text recognisable.
            settle_s=float(self.config.capture.get("settle_ms", 0) or 0) / 1000.0,
            max_boxes=int(self.config.ocr.get("max_boxes", 0) or 0),
            max_width=int(self.config.capture.get("max_width", 0)),
            target_lang=self.config.target_lang,
            source_lang=self.config.source_lang,
            min_confidence=float(self.config.translation.get("min_confidence", 0.0)),
        )
        self._pipeline = Pipeline(
            capturer=self._capturer,
            ocr=self._ocr,
            translator=self._translator,
            config=pipeline_config,
            on_update=self._on_update,
            on_stats=self._on_stats,
        )

    @property
    def pipeline(self) -> Pipeline:
        if self._pipeline is None:
            self.build()
        assert self._pipeline is not None
        return self._pipeline

    def load_plugins(self) -> list[Any]:
        """Discover and load plugins. A broken one is reported, never fatal."""
        if self.plugins_loaded:
            return self.plugins.plugins
        self.plugins_loaded = True
        if not self.config.plugins.get("enabled", True):
            return []
        directories = plugin_directories(self.config)
        self.plugins.load_all(directories)
        return self.plugins.plugins

    @property
    def translator(self) -> Translator:
        if self._translator is None:
            self.build()
        assert self._translator is not None
        return self._translator

    @property
    def ocr(self) -> RapidOcrEngine:
        if self._ocr is None:
            self.build()
        assert self._ocr is not None
        return self._ocr

    @property
    def capturer(self) -> Any:
        if self._capturer is None:
            self.build()
        return self._capturer

    @property
    def corpus(self) -> CorpusStore | None:
        return getattr(self.translator, "corpus", None)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._running:
            return
        self.build()
        self._started_at = time.perf_counter()
        self.pipeline.start(warmup=True)
        if getattr(self.translator, "model_available", False):
            start_worker = getattr(self.translator, "start", None)
            if callable(start_worker):
                start_worker()
        self._running = True
        self.publish(EVENT_READY, self.info())
        if self.hold_if_capturing_self():
            return
        self.set_status("正在监测屏幕区域…")

    # ------------------------------------------------------------------ #
    # not reading our own output
    # ------------------------------------------------------------------ #

    def find_self_over_region(self) -> list[tuple[Any, float]]:
        """Windows of this application sitting inside the region about to be recognised.

        The setting is consulted here rather than inside the detection, so that turning it
        off is a property of this method and not of whatever the detection happens to do --
        a check that stubs the detection must still see the switch work.
        """
        if not bool(self.config.capture.get("hold_if_self_visible", True)):
            return []
        return self._detect_self_windows()

    def _detect_self_windows(self) -> list[tuple[Any, float]]:
        """The actual look at the desktop: which of our windows are in the region.

        The overlay excludes itself from capture at the Windows level, which is the right
        fix for a window we own. A browser showing the web panel is not ours, so there is
        nothing to exclude -- the only honest options are to notice it or to read our own
        settings page over and over, which is what the user's startup stutter was.
        """
        try:
            from .winutil import own_ui_over
        except Exception:  # pragma: no cover - winutil is importable everywhere
            return []
        region = self._capture_region()
        if region is None:
            return []
        return own_ui_over(
            region,
            marker="Project Watashi",
            own_pid=os.getpid(),
            min_overlap=float(self.config.capture.get("self_overlap_warn", 0.15) or 0.15),
        )

    def _capture_region(self) -> Region | None:
        """The rectangle that will actually be photographed, in screen coordinates."""
        capturer = self._capturer
        region = getattr(capturer, "region", None) if capturer is not None else None
        if region is None:
            region = self._region
        if region is None:
            # No region configured: the capturer derives a bottom strip from the monitor,
            # and that is what has to be checked, not "nothing".
            try:
                monitor = list_monitors()[self.config.monitor - 1]
            except Exception:
                return None
            region = Region.bottom_strip(
                monitor,
                height_ratio=float(self.config.capture.get("region_ratio", 0.18) or 0.18),
            )
        return region if getattr(region, "valid", False) else None

    def hold_if_capturing_self(self) -> bool:
        """Pause before reading our own window, and explain why.

        Holding rather than warning-and-continuing is deliberate: the symptom of getting
        this wrong is not "a wrong subtitle", it is the program stuttering from the first
        frame because change detection never settles on a window that repaints its own
        counters. A user cannot act on a warning they are not looking at; they can act on
        "it did not start, and here is why".
        """
        findings = self.find_self_over_region()
        if not findings:
            return False
        window, ratio = findings[0]
        others = len(findings) - 1
        detail = (
            f"采集区域覆盖了本程序自己的窗口「{window.title}」"
            f"（占区域 {ratio * 100:.0f}%）"
            + (f"，另有 {others} 个" if others else "")
            + "。继续识别会读到自己的界面，画面每变一次就重新识别一次，"
            "所以先暂停；把该窗口移出区域后点「继续识别」即可。"
        )
        self._pause()
        self.set_status(detail)
        self.publish(EVENT_ERROR, {"command": "start", "message": detail})
        return True

    def warn_if_capturing_self(self) -> None:
        """Say it without holding. Used when the user has explicitly asked to resume."""
        findings = self.find_self_over_region()
        if not findings:
            return
        window, ratio = findings[0]
        self.set_status(
            f"提醒：采集区域仍覆盖「{window.title}」（{ratio * 100:.0f}%），"
            f"识别到的可能是本程序自己的界面。"
        )

    def _cmd_set_self_capture(self, payload: dict[str, Any]) -> str:
        """Turn the two self-capture guards on or off at runtime.

        ``hold_if_self_visible`` takes effect on the next check, which is now if it was
        just switched on -- the point of turning it on is that the engine is currently
        reading its own window.
        """
        parts: list[str] = []
        if "exclude_self" in payload:
            value = bool(payload["exclude_self"])
            self.config.capture["exclude_self"] = value
            # Said in both directions: a surface asks Windows for the flag when it creates
            # its window, so neither turning this on nor off does anything to a window
            # that is already up.
            parts.append(
                f"exclude_self={value} (takes effect for windows created from now on)"
            )
        if "hold_if_self_visible" in payload:
            value = bool(payload["hold_if_self_visible"])
            self.config.capture["hold_if_self_visible"] = value
            parts.append(f"hold_if_self_visible={value}")
            if value and self.hold_if_capturing_self():
                parts.append("(paused: the region covers our own window)")
        if not parts:
            raise ValueError(
                "set_self_capture needs 'exclude_self' and/or 'hold_if_self_visible'"
            )
        return " ".join(parts)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self.pipeline.stop()
        finally:
            stop_worker = getattr(self.translator, "stop", None)
            if callable(stop_worker):
                stop_worker()
        self.publish(EVENT_STOPPED, self.stats_dict())

    @property
    def running(self) -> bool:
        return self._running

    @property
    def uptime_s(self) -> float:
        return time.perf_counter() - self._started_at if self._started_at else 0.0

    # ------------------------------------------------------------------ #
    # event fan-out
    # ------------------------------------------------------------------ #

    def subscribe(self, maxsize: int = _SUBSCRIBER_LIMIT) -> queue.Queue:
        """Register an in-process subscriber (the overlay does this)."""
        channel: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._subscribers_lock:
            self._subscribers.append(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._subscribers_lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def publish(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        *,
        droppable: bool = True,
    ) -> dict[str, Any]:
        """Send an event to every subscriber. Returns the envelope sent."""
        envelope = encode_event(event_type, data, seq=self._next_seq())
        with self._subscribers_lock:
            channels = list(self._subscribers)
        for channel in channels:
            try:
                channel.put_nowait(envelope)
            except queue.Full:
                if not droppable:
                    # counters and errors matter more than a stale subtitle
                    try:
                        channel.get_nowait()
                        channel.put_nowait(envelope)
                    except queue.Empty:
                        pass
        return envelope

    def info(self) -> dict[str, Any]:
        """Everything a client needs to render an initial view."""
        backend_stats = self.translator.stats()
        window_title = getattr(self._capturer, "title", "") if self._capturer else ""
        capture_mode = (
            "window" if getattr(self._capturer, "KIND", "region") == "window" else "region"
        )
        return {
            "schema_version": SCHEMA_VERSION,
            #: so every surface can show what it is talking to, instead of a user
            #: having to guess which build is running
            "version": __version__,
            "release_stage": RELEASE_STAGE,
            "mode": self.config.overlay.get("mode"),
            "capture_mode": capture_mode,
            "capture_window": window_title,
            "region": (
                f"窗口：{window_title}" if window_title
                else str(self._region) if self._region
                else "auto (bottom strip)"
            ),
            "region_box": self._region_box(),
            "monitor": self.config.monitor,
            "fps_target": self.config.fps,
            "diff_threshold": self.config.capture.get("diff_threshold"),
            #: Stability gate in seconds; 0 means "recognise the changing frame".
            "settle_s": float(self.config.capture.get("settle_ms", 0) or 0) / 1000.0,
            "source_lang": self.config.source_lang,
            "target_lang": self.config.target_lang,
            "corpus_entries": int(backend_stats.get("corpus_entries", 0)),
            #: which target languages the corpus can actually answer for. Published
            #: rather than kept internal, because "nothing is being translated" is
            #: almost always "the vocabulary is for another language", and a user
            #: staring at untranslated subtitles has no other way to find that out.
            "corpus_languages": str(backend_stats.get("corpus_languages", "")),
            "rules": int(backend_stats.get("rules", 0)),
            "rule_ids": list(backend_stats.get("rule_ids", [])),
            "backend": backend_stats.get("backend", ""),
            "nmt_available": bool(backend_stats.get("nmt_available", False)),
            "nmt_error": backend_stats.get("nmt_error"),
            "nmt_load_ms": backend_stats.get("nmt_load_ms", 0.0),
            "ocr_load_ms": round(self.ocr.load_ms, 1),
            "paused": self.pipeline.paused if self._pipeline else False,
            "memory_mib": round(self._memory.sample(time.perf_counter()), 1),
            "presentation": self._presentation.to_dict(),
            "profile": self.config.profile,
            "profiles": [p.name for p in list_profiles(self.config)],
            "plugins": self.plugins.status(),
        }

    def _region_box(self) -> list[int] | None:
        """The capture region as structured [x, y, w, h].

        Clients need this to map per-line boxes (which are region relative) onto
        the screen, so it is structured rather than only the display string.

        The capturer is asked first: when capture is bound to a window the region
        is live geometry that moves, while ``self._region`` only describes a
        fixed rectangle.
        """
        region = None
        if self._capturer is not None:
            region = getattr(self._capturer, "region", None)
        if region is None:
            region = self._region
        if region is None:
            return None
        return [int(region.x), int(region.y), int(region.width), int(region.height)]

    def describe(self) -> Sequence[str]:
        return self.config.describe()

    # ------------------------------------------------------------------ #
    # pipeline callbacks (called on worker threads)
    # ------------------------------------------------------------------ #

    def _on_update(self, update: OverlayUpdate) -> None:
        self._last_update = update
        self._postprocess(update)
        self._remember(update)
        self.publish(EVENT_SUBTITLE, encode_update(update))

    def _postprocess(self, update: OverlayUpdate) -> None:
        """Run the plugin chain over each line, then rebuild the aggregate.

        Per line rather than over the joined text, so the ``lines`` array and the
        aggregate never disagree -- the overlay renders whichever the layout
        needs, and a mismatch would show two different translations of one line.
        """
        if not self._postprocess_enabled or not self.plugins.plugins:
            return
        context = {
            "target_lang": self.config.target_lang,
            "source_lang": self.config.source_lang,
            "backend": update.backend,
            "refined": update.refined,
        }
        for line in update.lines:
            if not line.target:
                continue
            cleaned = self.plugins.postprocess(line.target, context)
            if cleaned != line.target:
                line.target = cleaned
        if update.lines:
            update.target_text = "\n".join(line.target for line in update.lines)
        elif update.target_text:
            update.target_text = self.plugins.postprocess(update.target_text, context)

    def _remember(self, update: OverlayUpdate) -> None:
        """Keep the frame for the export extension point."""
        if not update.target_text.strip():
            return
        self.history.append(
            {
                "timestamp": update.timestamp,
                "source": update.source_text,
                "target": update.target_text,
                "latency_ms": update.latency_ms,
                "backend": update.backend,
                "refined": update.refined,
                "lines": [
                    {"source": line.source, "target": line.target} for line in update.lines
                ],
            }
        )

    def _on_stats(self, stats: OverlayStats) -> None:
        stats.memory_mib = self._memory.sample(time.perf_counter())
        self.publish(EVENT_STATS, encode_stats(stats), droppable=False)

    def _on_refined(self, refinement: Any) -> None:
        if self._postprocess_enabled and self.plugins.plugins:
            context = {
                "target_lang": self.config.target_lang,
                "backend": refinement.backend,
                "refined": True,
            }
            cleaned = self.plugins.postprocess(refinement.target_text, context)
            if cleaned != refinement.target_text:
                refinement.target_text = cleaned
        self.publish(EVENT_REFINEMENT, encode_refinement(refinement))

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def stats(self) -> OverlayStats:
        stats = self.pipeline.stats()
        stats.memory_mib = self._memory.sample(time.perf_counter())
        return stats

    def stats_dict(self) -> dict[str, Any]:
        data = encode_stats(self.stats())
        data.update(self.pipeline.to_dict())
        data["uptime_s"] = round(self.uptime_s, 1)
        return data

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #

    def command(self, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Apply a command and return a ``command_result`` payload."""
        payload = payload or {}
        try:
            handler = self._handlers().get(name)
            if handler is None:
                return command_result(name, False, f"unknown command {name!r}")
            detail = handler(payload)
            return command_result(name, True, detail or "ok", **self._post_state())
        except Exception as exc:
            self._last_error = f"{name}: {exc}"
            self.publish(EVENT_ERROR, {"command": name, "message": str(exc)})
            return command_result(name, False, f"{type(exc).__name__}: {exc}")

    def _handlers(self) -> dict[str, Callable[[dict[str, Any]], str]]:
        return {
            CMD_PAUSE: lambda _p: self._pause(),
            CMD_RESUME: lambda _p: self._resume(),
            CMD_TOGGLE_PAUSE: lambda _p: self._toggle_pause(),
            CMD_SET_REGION: self._cmd_set_region,
            CMD_USE_WINDOW: self._cmd_use_window,
            CMD_SET_TARGET_LANG: self._cmd_set_target_lang,
            CMD_SET_DIFF_THRESHOLD: self._cmd_set_diff_threshold,
            CMD_SET_FPS: self._cmd_set_fps,
            CMD_RELOAD_CORPUS: lambda _p: self._reload_corpus(),
            CMD_SET_PRESENTATION: self._cmd_set_presentation,
            CMD_LOAD_PROFILE: self._cmd_load_profile,
            CMD_EXPORT: self._cmd_export,
            CMD_STATUS: lambda _p: self._status_detail(),
            CMD_SHUTDOWN: lambda _p: self._shutdown(),
            CMD_CORRECT: self._cmd_correct,
            CMD_LIST_CORRECTIONS: self._cmd_list_corrections,
            CMD_REMOVE_CORRECTION: self._cmd_remove_correction,
            CMD_SET_CORPUS_RELOAD: self._cmd_set_corpus_reload,
            CMD_SET_SELF_CAPTURE: self._cmd_set_self_capture,
            CMD_LIBRARY_LIST: self._cmd_library_list,
            CMD_LIBRARY_PUT: self._cmd_library_put,
            CMD_LIBRARY_DELETE: self._cmd_library_delete,
            CMD_LIBRARY_SUPPRESS: self._cmd_library_suppress,
            CMD_LIBRARY_RESTORE: self._cmd_library_restore,
            CMD_LIBRARY_IMPORT: self._cmd_library_import,
            CMD_LIBRARY_EXPORT: self._cmd_library_export,
        }

    def _post_state(self) -> dict[str, Any]:
        try:
            return {
                "paused": self.pipeline.paused,
                "region": str(self._region) if self._region else None,
                "target_lang": self.config.target_lang,
            }
        except Exception:
            return {}

    def _pause(self) -> str:
        self.pipeline.pause()
        self.set_status("已暂停")
        return "paused"

    def _resume(self) -> str:
        self.pipeline.resume()
        # Told, not held: the user has decided to resume, so blocking them again would be
        # the program arguing with its user. They get the fact and can move the window.
        self.warn_if_capturing_self()
        if self._status.startswith("提醒："):
            return "resumed (the capture region still covers our own window)"
        self.set_status("正在监测屏幕区域…")
        return "resumed"

    def _toggle_pause(self) -> str:
        paused = self.pipeline.toggle_pause()
        self.set_status("已暂停" if paused else "正在监测屏幕区域…")
        return "paused" if paused else "resumed"

    def _cmd_set_region(self, payload: dict[str, Any]) -> str:
        raw = payload.get("region")
        if raw is None:
            raise ValueError("set_region needs a 'region' value")
        region = raw if isinstance(raw, Region) else Region.parse(str(raw))
        self._region = region
        if getattr(self.capturer, "KIND", "region") == "window":
            # The capturer follows a window and cannot honour a rectangle. A
            # dragged region means the user wants a rectangle, so the capture
            # source is replaced -- otherwise the drag looks accepted while the
            # window keeps being followed.
            self._capturer = RegionCapturer(
                region=region, monitor=self.config.monitor
            )
            self.pipeline.set_capturer(self._capturer)
            if self.hold_if_capturing_self():
                return f"region={region} (left window capture; paused: covers our own window)"
            self.set_status(f"区域已切换为 {region}")
            return f"region={region} (left window capture)"

        setter = getattr(self.capturer, "set_region", None)
        if callable(setter):
            setter(region)
        # a new region invalidates the current frame comparison
        self.pipeline.detector.reset()
        # A different rectangle can be a different answer to "does this cover our own
        # window", so the question is asked again -- otherwise moving the region off our
        # panel would leave the warning standing, and moving it on would say nothing.
        if self.hold_if_capturing_self():
            return f"region={region} (paused: the region covers our own window)"
        self.set_status(f"区域已切换为 {region}")
        return f"region={region}"

    def _cmd_use_window(self, payload: dict[str, Any]) -> str:
        """Switch capture to a whole window (or a fractional part of one).

        Unlike ``set_region`` this is not a coordinate snapshot: the capturer
        re-resolves the window's client area on every grab, so moving or resizing
        the target keeps the capture on it. That is the reason a window picker
        exists at all next to drag-to-select.
        """
        from .capture import WindowCapturer, WindowUnavailable

        spec = str(payload.get("spec") or payload.get("window") or "").strip()
        if not spec and not payload.get("hwnd"):
            raise ValueError("use_window needs a 'spec' (index or title) or 'hwnd'")

        sub_raw = payload.get("sub_region")
        sub = None
        if sub_raw:
            sub = (
                WindowCapturer.parse_sub_region(sub_raw)
                if isinstance(sub_raw, str)
                else tuple(float(v) for v in sub_raw)  # type: ignore[assignment]
            )

        capturer = WindowCapturer(
            hwnd=int(payload.get("hwnd") or 0),
            spec=spec,
            exclude_pid=os.getpid(),
            sub_region=sub,
        )
        try:
            capturer.resolve()
        except WindowUnavailable as exc:
            raise ValueError(f"window not found: {exc}") from exc

        self._capturer = capturer
        # the region is now derived from the window, so a fixed rectangle would be
        # a stale second source of truth for _region_box
        self._region = None
        self.pipeline.set_capturer(capturer)
        self.set_status(f"已切换为窗口捕获：{capturer.title or spec}")
        return f"window={capturer.title or spec} hwnd={capturer.hwnd}"

    def _cmd_set_target_lang(self, payload: dict[str, Any]) -> str:
        lang = str(payload.get("target_lang") or payload.get("value") or "").strip()
        if not lang:
            raise ValueError("set_target_lang needs a 'target_lang' value")
        self.config.translation["target"] = lang
        self.pipeline.config.target_lang = lang
        model = getattr(self.translator, "model", None)
        if model is not None:
            # cached refinements were for the old language
            cache = getattr(self.translator, "_cache", None)
            if isinstance(cache, dict):
                cache.clear()
        self.set_status(f"目标语言已切换为 {lang}")
        return f"target_lang={lang}"

    def _cmd_set_diff_threshold(self, payload: dict[str, Any]) -> str:
        value = float(payload.get("value"))
        self.config.capture["diff_threshold"] = value
        self.pipeline.detector.threshold = value
        return f"diff_threshold={value}"

    def _cmd_set_fps(self, payload: dict[str, Any]) -> str:
        value = float(payload.get("value"))
        if value <= 0:
            raise ValueError("fps must be positive")
        self.config.capture["fps"] = value
        self.pipeline.config.fps = value
        return f"fps={value}"

    def _cmd_set_corpus_reload(self, payload: dict[str, Any]) -> str:
        """Turn mtime hot reload on or off, and set how often it is checked.

        Separate from the settings panel on purpose: the panel's path also writes the
        override file, and a script that wants one run with reload off should not have
        to leave that decision in the user's config afterwards.
        """
        parts: list[str] = []
        corpus = self.corpus
        if "auto_reload" in payload:
            value = bool(payload["auto_reload"])
            self.config.corpus["auto_reload"] = value
            if corpus is not None:
                corpus.auto_reload = value
            parts.append(f"auto_reload={value}")
        if "reload_interval_ms" in payload:
            value = max(0, int(payload["reload_interval_ms"]))
            self.config.corpus["reload_interval_ms"] = value
            if corpus is not None:
                corpus.reload_interval_s = value / 1000.0
            parts.append(f"reload_interval_ms={value}")
        if not parts:
            raise ValueError("set_corpus_reload needs 'auto_reload' and/or 'reload_interval_ms'")
        return " ".join(parts)

    def _reload_corpus(self) -> str:
        corpus = self.corpus
        if corpus is None:
            raise ValueError("this translator has no corpus to reload")
        corpus.load()
        self.set_status(f"语料库已重载：{corpus.size} 条 / {corpus.rule_count} 条规则")
        return f"entries={corpus.size} rules={corpus.rule_count}"

    # ------------------------------------------------------------------ #
    # real time correction
    # ------------------------------------------------------------------ #

    def _cmd_correct(self, payload: dict[str, Any]) -> str:
        """Record a human correction and make it visible before returning.

        The five steps are the whole feature, and skipping any one of them leaves a
        correction that appears to work and does not:

        1. **write** it to the user corpus layer (a file, so it survives a restart);
        2. **load** it, forcing rather than waiting for the reload throttle -- the user
           is looking at the screen to see whether their fix took;
        3. **forget** what is remembered about that text: the reuse memory, and any
           cached model refinement. Either one would be served instead of the
           correction, and the refinement cache would keep serving it indefinitely;
        4. **repaint** the frame on screen, because a still screenshot produces no new
           frame at all: without this, a correction made on a paused or static screen
           would sit in the corpus and never be shown;
        5. **outlive anything already in flight.** A refinement the model started
           before the correction is discarded rather than published over it (see
           ``CorpusStore.revision``), because the answer to "did my correction work"
           must not depend on which thread finished first.
        """
        source = str(payload.get("source") or "").strip()
        target = str(payload.get("target") or payload.get("translation") or "").strip()
        if not source:
            raise ValueError("correct 需要 'source'：屏幕上被识别出的原文")
        if not target:
            raise ValueError("correct 需要 'target'：你希望它显示的译文")
        scope = str(payload.get("scope") or corrections.SCOPE_LINE).strip().lower()

        corpus = self.corpus
        store = getattr(corpus, "corrections", None) if corpus is not None else None
        if store is not None:
            # The engine's own store, not a path derived from config. The two agree
            # whenever the corpus was built from this config, but "agree whenever"
            # is how a correction ends up written somewhere the engine never reads:
            # stored, reported as saved, and completely without effect.
            summary = store.apply(source, target, scope=scope, note=payload.get("note"))
        else:
            summary = corrections.record(
                self.config, source, target, scope=scope, note=payload.get("note")
            )

        if corpus is not None:
            corpus.load()
        forgotten = self._forget_correction(source, scope)
        summary["forgotten"] = forgotten
        if scope == corrections.SCOPE_TERM:
            summary["engages"] = "every occurrence of this term, in any sentence"
        elif len(corrections.loose_normalize(source)) >= corrections.MIN_KEY_LENGTH:
            summary["engages"] = "every frame that reads this sentence, punctuation aside"
        else:
            summary["engages"] = (
                "this exact sentence only: too short to match loosely without "
                "colliding with other lines"
            )

        repainted, update = self._repaint_for_correction(source, target, scope)
        summary["repainted_lines"] = repainted

        # The correction is announced before the repainted frame. A surface that lists
        # corrections reloads that list on this event, and if the repaint arrived first
        # that reload could paint over the frame it had just been given.
        self.publish(EVENT_CORRECTION, summary, droppable=False)
        if update is not None and repainted:
            self.publish(EVENT_SUBTITLE, encode_update(update))

        where = Path(summary["path"]).name
        self.set_status(
            f"已记录纠正（{scope}）：{source} → {target}；已写入 {where}"
        )
        return (
            f"{'created' if summary['created'] else 'updated'} {scope} correction "
            f"-> {summary['path']} (total {summary['total']}, repainted {repainted} line(s))"
        )

    def _forget_correction(self, source: str, scope: str) -> dict[str, int]:
        """Make the corrected text unremembered, in both memories that hold it.

        Called for a correction and for its removal: undo has to be as visible as the
        change it undoes, and a cache that still holds the corrected answer would make
        deleting the correction look like it did nothing.
        """
        forgotten = {"recent": 0, "refinements": 0}
        if self._pipeline is not None:
            recent = getattr(self._pipeline, "recent", None)
            if recent is not None:
                forgotten["recent"] = (
                    recent.drop(source)
                    if scope == corrections.SCOPE_LINE
                    else recent.drop_containing(source)
                )
        translator = self._translator
        forget = getattr(translator, "forget", None)
        if callable(forget):
            if scope == corrections.SCOPE_LINE:
                forgotten["refinements"] = forget(source=source)
            else:
                # a term lives inside a line, so the stale entry is that whole line
                forgotten["refinements"] = forget(contains=source)
        return forgotten

    def _repaint_for_correction(
        self, source: str, target: str, scope: str
    ) -> tuple[int, Any | None]:
        """Fix what is on screen now, without OCR and without the model.

        A ``line`` correction knows the finished sentence, so it is written straight
        onto the line. A ``term`` correction only knows one word, so the line is
        re-run through the corpus tier -- which is instant, and is the tier that owns
        terminology -- rather than being rebuilt around a word the user never saw in
        context. Either way the model is not consulted: its opinion is what produced
        the text being corrected.

        Returns how many lines changed and the update to republish, if any.
        """
        update = self._last_update
        if update is None or not update.lines:
            return 0, None

        touched = 0
        for line in update.lines:
            if scope == corrections.SCOPE_LINE:
                if corrections.loose_normalize(line.source) != corrections.loose_normalize(source):
                    continue
                line.target = target
                # by definition: the human's answer covers the whole sentence
                line.coverage = 1.0
                line.confidence = 1.0
            else:
                if source not in line.source:
                    continue
                corpus = self.corpus
                if corpus is None:
                    continue
                outcome = corpus.translate(line.source, self.config.target_lang)
                line.target = outcome.target_text
                # *not* forced to 1.0: correcting one word does not make the rest of the
                # line covered, and claiming it does would hide the parts that are not
                line.coverage = outcome.coverage
                line.confidence = outcome.confidence
            touched += 1

        if not touched:
            return 0, None

        update.target_text = "\n".join(line.target for line in update.lines)
        self._rewrite_history(source, target, scope)
        return touched, update

    def _rewrite_history(self, source: str, target: str, scope: str) -> None:
        """Update the recorded frames, so the history does not keep the old answer.

        The history is what the user re-reads and what an export writes out; leaving
        the rejected translation in it would put two different answers in front of the
        same sentence.
        """
        for item in self.history:
            if scope == corrections.SCOPE_LINE:
                if corrections.loose_normalize(str(item.get("source", ""))) != corrections.loose_normalize(source):
                    continue
                item["target"] = target
            else:
                lines = item.get("lines") or []
                for line in lines:
                    if isinstance(line, dict) and source in str(line.get("source", "")):
                        corpus = self.corpus
                        if corpus is None:
                            continue
                        line["target"] = corpus.translate(
                            str(line["source"]), self.config.target_lang
                        ).target_text
                if lines:
                    item["target"] = "\n".join(
                        str(line.get("target", "")) for line in lines if isinstance(line, dict)
                    )
            item["corrected"] = True

    def _cmd_list_corrections(self, _payload: dict[str, Any]) -> str:
        items = self.correction_listing()
        if not items:
            return "no corrections recorded yet"
        return f"{len(items)} correction(s): " + "; ".join(
            f"{c['source']} → {c['target']} [{c['scope']}]" for c in items[:5]
        )

    def correction_listing(self) -> list[dict[str, Any]]:
        """Every recorded correction, with the hit counts of this run when we own them."""
        live = self.corpus
        store = getattr(live, "corrections", None) if live is not None else None
        if store is not None:
            return store.describe()
        return corrections.listing(self.config)

    def _cmd_remove_correction(self, payload: dict[str, Any]) -> str:
        source = str(payload.get("source") or "").strip()
        if not source:
            raise ValueError("remove_correction 需要 'source'")
        # Through the engine's own store when there is one, for the same reason the
        # correction was written there: a delete aimed at a different file than the
        # one the engine reads would report success and change nothing.
        corpus = self.corpus
        store = getattr(corpus, "corrections", None) if corpus is not None else None
        scope = corrections.SCOPE_LINE
        if store is not None:
            known = store.lookup_exact(source)
            if known is not None:
                scope = known.scope
            removed = store.remove(source)
        else:
            removed = corrections.remove(self.config, source)
        if not removed:
            raise ValueError(f"没有找到针对 {source!r} 的纠正记录")
        if corpus is not None:
            corpus.load()
        self._forget_correction(source, scope)
        self.publish(
            EVENT_CORRECTION,
            {"removed": source, "total": len(self.correction_listing())},
            droppable=False,
        )
        self.set_status(f"已删除纠正：{source}")
        return f"removed {source}"

    # ------------------------------------------------------------------ #
    # the corpus editor
    # ------------------------------------------------------------------ #

    def library(self) -> "library_module.Library | None":
        """The edited corpus file, created on first use.

        Held on the session rather than rebuilt per call: the file is small, but a UI
        that lists and then edits wants the same object, and a reload after every write
        keeps it in step with what the engine actually loaded.
        """
        if self._library is None:
            self._library = library_module.Library(
                library_module.library_path(self.config)
            )
        return self._library

    def library_view(self) -> dict[str, Any]:
        """Everything a corpus editor shows: entries from every layer, and what is hidden."""
        store = self.library()
        assert store is not None
        return library_module.corpus_view(self.corpus, store)

    def _after_library_change(self, detail: str) -> dict[str, Any]:
        """Make an edited corpus take effect now, and tell every surface.

        The same discipline as a correction: write, force a reload (the throttle is for
        mtime changes noticed in passing, not for a change the user just made and is
        watching for), and publish so the other surface repaints. Missing the reload is
        how "I edited it and nothing happened" becomes a bug report about caching.
        """
        corpus = self.corpus
        if corpus is not None:
            corpus.load()
        view = self.library_view()
        self.publish(
            EVENT_LIBRARY,
            {"detail": detail, "active": view["active"], "user": view["user"]},
            droppable=False,
        )
        return view

    def _cmd_library_list(self, _payload: dict[str, Any]) -> str:
        view = self.library_view()
        return (
            f"{view['active']} active ({view['user']} of them yours, "
            f"{view['overriding']} overriding a shipped entry), "
            f"{view['suppressed']} hidden"
        )

    def _cmd_library_put(self, payload: dict[str, Any]) -> str:
        source = str(payload.get("source") or "").strip()
        target = str(payload.get("target") or payload.get("translation") or "").strip()
        if not source:
            raise ValueError("library_put 需要 'source'：要改的词（原文）")
        if not target:
            raise ValueError("library_put 需要 'target'：这个词应该译成什么")
        store = self.library()
        assert store is not None
        before = store.entries.get(source)
        entry, created = store.put(
            source,
            target,
            lang=str(payload.get("lang") or "").strip() or None,
            pos=str(payload.get("pos") or "").strip() or None,
            domain=str(payload.get("domain") or "").strip() or None,
            note=str(payload.get("note") or "").strip() or None,
        )
        view = self._after_library_change(f"{'added' if created else 'updated'} {source}")
        row = next(
            (item for item in view["entries"] if item["source"] == entry.source), None
        )
        overrode = row["overrides"] if row else None
        if overrode:
            self.set_status(
                f"已覆盖出厂词条：{source} → {target}（原为 {overrode}），"
                f"出厂文件未被修改"
            )
        else:
            self.set_status(f"已{'新增' if created else '修改'}词条：{source} → {target}")
        return (
            f"{'created' if created else 'updated'} {source!r} -> {target!r}"
            + (f", overriding {overrode}" if overrode else "")
            + (f" (was {before.target!r})" if before else "")
        )

    def _cmd_library_delete(self, payload: dict[str, Any]) -> str:
        source = str(payload.get("source") or "").strip()
        if not source:
            raise ValueError("library_delete 需要 'source'")
        store = self.library()
        assert store is not None
        if not store.delete(source):
            raise ValueError(f"你的语料库里没有 {source!r}（出厂词条请用 library_suppress）")
        view = self._after_library_change(f"deleted {source}")
        row = next(
            (item for item in view["entries"] if item["source"] == source), None
        )
        if row is not None:
            # The shipped entry underneath is visible again: that is what a revert is.
            self.set_status(f"已撤销覆盖：{source} 恢复为出厂译文「{row['target']}」")
            return f"reverted {source} to {row['origin']} -> {row['target']!r}"
        self.set_status(f"已删除词条：{source}")
        return f"deleted {source}"

    def _cmd_library_suppress(self, payload: dict[str, Any]) -> str:
        source = str(payload.get("source") or "").strip()
        if not source:
            raise ValueError("library_suppress 需要 'source'")
        store = self.library()
        assert store is not None
        if not store.suppress(source):
            raise ValueError(f"{source!r} 已经是停用状态，或者本就不存在")
        self._after_library_change(f"suppressed {source}")
        self.set_status(f"已停用出厂词条：{source}")
        return f"suppressed {source}"

    def _cmd_library_restore(self, payload: dict[str, Any]) -> str:
        source = str(payload.get("source") or "").strip()
        if not source:
            raise ValueError("library_restore 需要 'source'")
        store = self.library()
        assert store is not None
        restored = store.unsuppress(source)
        # An override is also a reason the shipped entry is not what you see, so restore
        # clears both. Two buttons that each fix half the problem is how a user concludes
        # the button does not work.
        also = store.delete(source)
        if not restored and not also:
            raise ValueError(f"{source!r} 既没有停用也没有覆盖，无需还原")
        self._after_library_change(f"restored {source}")
        self.set_status(f"已还原为出厂状态：{source}")
        return f"restored {source}"

    def _cmd_library_import(self, payload: dict[str, Any]) -> str:
        text = str(payload.get("text") or "")
        fmt = str(payload.get("format") or payload.get("fmt") or "json").strip().lower()
        if not text.strip():
            raise ValueError("library_import 需要 'text'：文件内容")
        store = self.library()
        assert store is not None

        entries, problems = library_module.parse_import(text, fmt)
        if not entries and fmt not in library_module.EXPORT_FORMATS:
            # A format we do not know: the reserved plugin hook is exactly this case.
            entries, problems = self._import_via_plugin(text, fmt)
        if not entries:
            raise ValueError(
                f"没能从这个文件里读出词条（{fmt}）："
                + ("；".join(problems[:3]) if problems else "格式不支持")
            )

        # Entries that matter are protected by default, so an import is additive unless
        # the caller says otherwise: replacing vocabulary is a deliberate act.
        result = store.merge(
            entries, replace=bool(payload.get("replace", True))
        )
        problems = list(problems) + [f"skipped {name}" for name in result["skipped_sources"]]
        view = self._after_library_change(
            f"imported {result['added']} added, {result['updated']} updated"
        )
        self.set_status(
            f"已导入 {len(entries)} 条：新增 {result['added']}、更新 {result['updated']}"
            + (f"、跳过 {result['skipped']}" if result["skipped"] else "")
        )
        return (
            f"imported {len(entries)} entries: added {result['added']}, "
            f"updated {result['updated']}, skipped {result['skipped']}, "
            f"total {view['user']} of yours"
            + (f"; notes: {'; '.join(result['skipped_sources'][:3])}"
               if result["skipped"] else "")
        )

    def _import_via_plugin(
        self, text: str, fmt: str
    ) -> tuple[list[Any], list[str]]:
        """Let a plugin read a format we do not know.

        The text arrives from a file dialog, not from a path, so it is written to a
        temporary file with the caller's suffix and handed to the plugin: that is what
        ``corpus_loader(path) -> dict`` asks for, and rewiring the API to take text would
        break every loader that wants to open a database next to it.
        """
        import tempfile

        suffix = f".{fmt}" if fmt and fmt.isalnum() else ".corpus"
        self.load_plugins()
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=suffix, delete=False, encoding="utf-8"
        )
        try:
            handle.write(text)
            handle.close()
            payload, error = self.plugins.load_corpus(Path(handle.name))
        finally:
            Path(handle.name).unlink(missing_ok=True)
        if payload is None:
            return [], [error or "the plugin returned nothing"]
        entries, problems = library_module.entries_from_payload(payload)
        return entries, problems

    def _cmd_library_export(self, payload: dict[str, Any]) -> str:
        fmt = str(payload.get("format") or payload.get("fmt") or "json").strip().lower()
        scope = str(payload.get("scope") or "effective").strip().lower()
        if fmt not in library_module.EXPORT_FORMATS:
            raise ValueError(
                f"导出格式不支持 {fmt!r}；可用：{', '.join(library_module.EXPORT_FORMATS)}"
            )
        if scope not in ("user", "effective"):
            raise ValueError("scope 只能是 'user'（只导出你写的）或 'effective'（导出实际生效的）")
        corpus = self.library()
        assert corpus is not None
        rows = library_module.effective_entries(self.corpus, corpus, scope)
        text = library_module.to_export(rows, fmt)
        path = payload.get("path")
        if path:
            target = Path(str(path))
            if not target.is_absolute():
                target = self.config.base_dir / target
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            return f"format={fmt} scope={scope} entries={len(rows)} written={target}"
        return f"format={fmt} scope={scope} entries={len(rows)} chars={len(text)}"

    def library_export_text(self, fmt: str = "json", scope: str = "effective") -> str:
        """The exported corpus as text, for a surface that wants to hand it to the user.

        Separate from the command because a download needs the *text*, and the command
        result is a one-line detail string. Validated here so both paths reject the same
        inputs, rather than the web endpoint being the only one that checks.
        """
        if fmt not in library_module.EXPORT_FORMATS:
            raise ValueError(
                f"导出格式不支持 {fmt!r}；可用：{', '.join(library_module.EXPORT_FORMATS)}"
            )
        if scope not in ("user", "effective"):
            raise ValueError("scope 只能是 'user' 或 'effective'")
        store = self.library()
        assert store is not None
        rows = library_module.effective_entries(self.corpus, store, scope)
        return library_module.to_export(rows, fmt)

    def _cmd_set_presentation(self, payload: dict[str, Any]) -> str:
        """Swap the look at runtime, from any surface.

        Accepts either a preset name (``{"preset": "inplace"}``) or a full or
        partial spec (``{"spec": {...}}``), which is merged over the current one
        so a UI changing only the background need not resend everything.
        """
        if "preset" in payload and payload["preset"]:
            self._presentation = PresentationSpec.preset(str(payload["preset"]))
        elif isinstance(payload.get("spec"), dict):
            self._presentation = self._presentation.merged(payload["spec"])
        elif isinstance(payload.get("name"), str) and payload["name"] in PresentationSpec.preset_names():
            self._presentation = PresentationSpec.preset(payload["name"])
        else:
            raise ValueError("set_presentation needs 'preset', 'name' or a 'spec' object")

        if self._presentation.needs_geometry and not self._region:
            self.set_status("提示：原位覆盖需要先选定屏幕区域")
        self._publish_presentation()
        return (
            f"presentation={self._presentation.name} "
            f"mode={self._presentation.layout.mode}"
        )

    def _cmd_export(self, payload: dict[str, Any]) -> str:
        """Run an export plugin over the session history.

        The text is kept on the session as well as summarised in the result: a UI
        wants the text to display, a script wants the file, and the result field
        is only a short detail string.
        """
        fmt = str(payload.get("format") or payload.get("fmt") or "").strip()
        if not fmt:
            available = ", ".join(self.plugins.export_formats()) or "(none registered)"
            raise ValueError(f"export needs a 'format'; available: {available}")

        entries = self._sequenced_history()
        text = self.plugins.export(fmt, {"entries": entries}, payload.get("options") or {})
        if text is None:
            available = ", ".join(self.plugins.export_formats()) or "(none registered)"
            raise ValueError(
                f"no plugin provides export format {fmt!r}; available: {available}"
            )
        self._last_export = text

        path = payload.get("path")
        if path:
            target = Path(str(path))
            if not target.is_absolute():
                target = self.config.base_dir / target
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            return f"format={fmt} entries={len(entries)} written={target}"
        return f"format={fmt} entries={len(entries)} chars={len(text)}"

    def _sequenced_history(self) -> list[dict[str, Any]]:
        """History with subtitle timings derived from when each frame appeared.

        Offsets are taken from the *first* frame, not from the previous one --
        treating the gap since the last frame as an absolute start would drift.
        Each cue ends when the next one begins, so cues do not overlap; the last
        one gets a nominal duration.
        """
        entries: list[dict[str, Any]] = []
        stamps: list[float | None] = []
        for item in self.history:
            stamp = item.get("timestamp")
            entries.append(
                {
                    "source": item.get("source", ""),
                    "target": item.get("target", ""),
                    "timestamp": stamp,
                }
            )
            stamps.append(float(stamp) if stamp is not None else None)

        base = next((s for s in stamps if s is not None), 0.0)
        offsets = [0.0 if s is None else max(0.0, s - base) for s in stamps]
        for index, (entry, start) in enumerate(zip(entries, offsets)):
            following = offsets[index + 1] if index + 1 < len(offsets) else None
            end = start + 2.0
            if following is not None and following > start:
                end = min(end, following)
            entries[index] = {**entry, "start": start, "end": max(start + 0.2, end)}
        return entries

    def export(self, fmt: str, path: Any = None, options: dict[str, Any] | None = None) -> str | None:
        """Convenience wrapper returning the exported text, or None."""
        result = self.command(
            CMD_EXPORT, {"format": fmt, "path": path, "options": options or {}}
        )
        if not result.get("ok"):
            raise ValueError(result.get("detail") or "export failed")
        return self._last_export

    def plugin_status(self) -> dict[str, Any]:
        return self.plugins.status()

    def _cmd_load_profile(self, payload: dict[str, Any]) -> str:
        from .profiles import apply_profile, find_profile, list_profiles

        name = str(payload.get("name") or payload.get("profile") or "").strip()
        if not name:
            raise ValueError("load_profile needs a 'name'")
        profile = find_profile(self.config, name)
        if profile is None:
            available = ", ".join(p.name for p in list_profiles(self.config)) or "(none)"
            raise ValueError(f"no profile named {name!r}; available: {available}")
        changes = apply_profile(self.config, profile)
        self._presentation = resolve_presentation(self.config)
        self.publish(EVENT_PRESENTATION, self._presentation.to_dict(), droppable=False)
        for sink in list(self.presentation_sinks):
            try:
                sink(self._presentation)
            except Exception:
                pass
        self._reload_corpus()
        detail = f"profile={profile.name}"
        if changes:
            detail += f" ({'; '.join(changes)})"
        self.set_status(f"已切换配置档：{profile.name}")
        return detail

    def _status_detail(self) -> str:
        return self._status or "ok"

    def _shutdown(self) -> str:
        # the caller owns the main loop, so this only signals intent
        self.publish(EVENT_STATUS, {"message": "shutdown requested"})
        return "shutdown requested"

    def apply_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Change settings from any surface: memory, disk, and tell the others.

        One path for every surface, because the three steps have to happen together:

        * **memory** -- the running engine and every surface read ``config``, so
          writing only the file would leave the desktop window showing the old value
          until the next start. That gap is what "联动" means in practice.
        * **disk** -- the override file, or a setting marked "restart to apply"
          would be forgotten at exactly the moment it is supposed to matter.
        * **the event** -- so a change made in the web panel repaints the desktop
          window without either surface knowing the other exists.

        Validation happens before anything is mutated, so a batch containing one bad
        value changes nothing.
        """
        from . import settings_schema

        if not isinstance(changes, dict) or not changes:
            return {"ok": False, "detail": "no changes given"}

        unknown = sorted(k for k in changes if settings_schema.find(k) is None)
        if unknown:
            return {"ok": False, "detail": f"unknown setting(s): {', '.join(unknown)}"}

        coerced: dict[str, Any] = {}
        rejected: dict[str, str] = {}
        for key, raw in changes.items():
            field = settings_schema.find(key)
            assert field is not None
            try:
                coerced[key] = settings_schema.coerce(field, raw)
            except ValueError as exc:
                rejected[key] = str(exc)
        if rejected:
            return {"ok": False, "rejected": rejected}

        for key, value in coerced.items():
            section, name = settings_schema.split(key)
            if not section:
                setattr(self.config, name, value)
                continue
            container = getattr(self.config, section, None)
            if isinstance(container, dict):
                container[name] = value
            else:
                setattr(self.config, section, value)
            if key == "capture.region":
                # the engine holds a parsed Region, not the string; leaving it stale
                # would make the panel disagree with what is actually captured
                self.config.region = Region.parse(value) if value else None

        written = self.config.save_overrides(coerced)

        # Settings that an already built engine holds a copy of, pushed through here.
        # Storing them and marking them "live" is not the same as applying them: the
        # corpus was handed its reload policy when it was constructed, so a change
        # that only reached config would look applied and do nothing until restart.
        corpus = self.corpus
        if corpus is not None:
            if "corpus.auto_reload" in coerced:
                corpus.auto_reload = coerced["corpus.auto_reload"]
            if "corpus.reload_interval_ms" in coerced:
                corpus.reload_interval_s = float(coerced["corpus.reload_interval_ms"]) / 1000.0

        live, deferred = [], []
        for key in coerced:
            field = settings_schema.find(key)
            assert field is not None
            (live if field.applies == settings_schema.LIVE else deferred).append(key)

        self.publish(
            EVENT_SETTINGS,
            {
                "changed": sorted(coerced),
                "applied_now": sorted(live),
                "needs_restart": sorted(deferred),
                "values": {k: coerced[k] for k in sorted(coerced)},
            },
            droppable=False,
        )
        self.set_status("设置已更新：" + "、".join(sorted(coerced)))
        return {
            "ok": True,
            "written": written,
            "applied_now": sorted(live),
            "needs_restart": sorted(deferred),
            "values": {k: coerced[k] for k in sorted(coerced)},
        }

    def settings_payload(self) -> dict[str, Any]:
        """The settings schema with current values, for any surface to render.

        Shared rather than duplicated per surface: two renderers reading two
        different snapshots is how the panel and the window start disagreeing.
        """
        from . import settings_schema

        payload = settings_schema.as_dict(self.config)
        overrides = self.config.read_overrides()
        changed: list[str] = []
        for section, values in overrides.items():
            if isinstance(values, dict):
                changed.extend(f"{section}.{name}" for name in values)
            else:
                changed.append(section)
        payload["overridden"] = sorted(changed)
        payload["config_file"] = str(self.config.base_dir / "config.yaml")
        payload["overrides_file"] = str(AppConfig.overrides_path(self.config.base_dir))
        return payload

    def set_status(self, message: str) -> None:
        self._status = message
        self.publish(EVENT_STATUS, {"message": message}, droppable=False)


# --------------------------------------------------------------------------- #
# builders (shared by Session and by the CLI when it needs pieces directly)
# --------------------------------------------------------------------------- #


def build_ocr(config: AppConfig) -> RapidOcrEngine:
    return RapidOcrEngine(
        max_width=int(config.capture.get("max_width", 0)),
        use_cls=bool(config.ocr.get("use_cls", False)),
        intra_op_threads=int(config.ocr.get("intra_op_threads", 4)),
        inter_op_threads=int(config.ocr.get("inter_op_threads", 1)),
        use_mem_arena=bool(config.ocr.get("use_mem_arena", True)),
        det_limit_type=str(config.ocr.get("det_limit_type", "max")),
        det_limit_side_len=config.ocr.get("det_limit_side_len"),
    )


def build_corpus(config: AppConfig) -> CorpusStore:
    return CorpusStore(
        layers={
            LAYER_USER: config.corpus_dirs("user"),
            LAYER_DOMAIN: config.corpus_dirs("domain"),
            LAYER_GENERAL: config.corpus_dirs("general"),
        },
        rule_files=config.rule_files(),
        auto_reload=bool(config.corpus.get("auto_reload", True)),
        reload_interval_s=float(config.corpus.get("reload_interval_ms", 500) or 0) / 1000.0,
    )


def build_translator(
    config: AppConfig,
    on_refined: Callable[[Any], None] | None = None,
) -> Translator:
    """Build the hybrid translator: corpus fast path plus optional local model.

    Always returns a ``HybridTranslator``; with no model configured or an
    incomplete model directory it degrades to corpus + rules and reports why,
    so the application never depends on the model being present.
    """
    from .local_nmt import NmtModel, load_hybrid_translator

    corpus = build_corpus(config)
    protect = bool(config.translation.get("protect_terms", True))
    min_confidence = float(config.translation.get("protect_min_confidence", 0.4))

    translator = load_hybrid_translator(
        corpus=corpus,
        model_dir=None,
        on_refined=on_refined,
        protect_terms=protect,
        source_lang=config.source_lang,
        protect_min_confidence=min_confidence,
    )

    model_path = config.translation.get("nmt_model")
    if model_path:
        resolved = Path(str(model_path))
        if not resolved.is_absolute():
            resolved = config.base_dir / resolved
        model = NmtModel(
            resolved,
            compute_type=str(config.translation.get("nmt_compute_type", "int8")),
            beam_size=int(config.translation.get("nmt_beam_size", 1)),
            intra_threads=int(config.translation.get("nmt_intra_threads", 4)),
        )
        print(f"  loading local model: {resolved}")
        if model.load():
            print(
                f"  model ready in {model.load_ms:.0f} ms "
                f"({model.compute_type}, beam {model.beam_size})"
            )
        else:
            print("  model unavailable, continuing with corpus + rules only:")
            print(f"    {model.load_error}")
        translator.model = model
        translator.protect_min_confidence = min_confidence

    return translator
