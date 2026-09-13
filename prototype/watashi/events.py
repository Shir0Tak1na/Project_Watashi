"""Transport agnostic event and command schema -- the engine's UI boundary.

Everything in this module is pure data and plain Python: **no tkinter, no
network, no model imports**. That is the point. The engine publishes events and
accepts commands; how those travel is somebody else's problem:

::

    engine (session.py)
        |
        |  events: ready, subtitle, refinement, line, stats, status, error
        |  commands: pause, resume, set_region, reload_corpus, ...
        v
    +----------------+----------------+----------------+----------------+
    | CLI / PowerShell | floating overlay | local web panel | (future) LAN  |
    |  JSON lines      |  in-process     |  SSE + POST     |  same schema  |
    +----------------+----------------+----------------+----------------+

Because the schema is fixed here, adding a surface means writing an adapter, not
another copy of the app. The overlay, the web panel and `--json-lines` all
consume exactly the same events.

Versioning: ``SCHEMA_VERSION`` is carried on every envelope. Bump it when a
field changes meaning, so a client can refuse a stream it does not understand
instead of silently misreading subtitles.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------- #
# event types
# --------------------------------------------------------------------------- #

EVENT_READY = "ready"
EVENT_SUBTITLE = "subtitle"
EVENT_REFINEMENT = "refinement"
EVENT_STATS = "stats"
EVENT_STATUS = "status"
EVENT_ERROR = "error"
EVENT_COMMAND_RESULT = "command_result"
EVENT_STOPPED = "stopped"
EVENT_PRESENTATION = "presentation"
#: A setting changed on some surface. Every other surface re-reads the config on
#: this, which is what keeps the desktop window and the web panel showing the same
#: values instead of each drifting from the file at its own pace.
EVENT_SETTINGS = "settings"
#: A human correction was recorded. The subtitle event that carries the fixed text
#: travels separately, so a surface that only wants to update its list of corrections
#: need not re-render a frame.
EVENT_CORRECTION = "correction"

ALL_EVENTS = (
    EVENT_READY,
    EVENT_SUBTITLE,
    EVENT_REFINEMENT,
    EVENT_STATS,
    EVENT_STATUS,
    EVENT_ERROR,
    EVENT_SETTINGS,
    EVENT_CORRECTION,
    EVENT_COMMAND_RESULT,
    EVENT_STOPPED,
    EVENT_PRESENTATION,
)

# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

CMD_PAUSE = "pause"
CMD_RESUME = "resume"
CMD_TOGGLE_PAUSE = "toggle_pause"
CMD_SET_REGION = "set_region"
CMD_USE_WINDOW = "use_window"
CMD_SET_TARGET_LANG = "set_target_lang"
CMD_SET_DIFF_THRESHOLD = "set_diff_threshold"
CMD_SET_FPS = "set_fps"
CMD_RELOAD_CORPUS = "reload_corpus"
CMD_SET_PRESENTATION = "set_presentation"
CMD_LOAD_PROFILE = "load_profile"
CMD_EXPORT = "export"
CMD_STATUS = "status"
CMD_SHUTDOWN = "shutdown"
#: record a human correction: {"source", "target", "scope", "note"}
CMD_CORRECT = "correct"
CMD_LIST_CORRECTIONS = "list_corrections"
CMD_REMOVE_CORRECTION = "remove_correction"
#: {"auto_reload": bool, "reload_interval_ms": int}
CMD_SET_CORPUS_RELOAD = "set_corpus_reload"

ALL_COMMANDS = (
    CMD_PAUSE,
    CMD_RESUME,
    CMD_TOGGLE_PAUSE,
    CMD_SET_REGION,
    CMD_USE_WINDOW,
    CMD_SET_TARGET_LANG,
    CMD_SET_DIFF_THRESHOLD,
    CMD_SET_FPS,
    CMD_RELOAD_CORPUS,
    CMD_SET_PRESENTATION,
    CMD_LOAD_PROFILE,
    CMD_EXPORT,
    CMD_STATUS,
    CMD_SHUTDOWN,
    CMD_CORRECT,
    CMD_LIST_CORRECTIONS,
    CMD_REMOVE_CORRECTION,
    CMD_SET_CORPUS_RELOAD,
)


# --------------------------------------------------------------------------- #
# payload dataclasses (moved out of overlay.py so the engine needs no UI toolkit)
# --------------------------------------------------------------------------- #


@dataclass
class TranslatedLine:
    """One recognised line with its translation and screen geometry.

    ``box`` is ``(x, y, width, height)`` in **region relative** coordinates. A
    renderer maps it to the screen by adding the region origin, which clients get
    from the ``ready`` event. Geometry is optional: synthetic frame sources and
    any non-OCR source may legitimately have none.

    Carrying this is what makes ``inplace`` layout ("draw the translation over
    the original text") expressible at all. Without it the only possible layouts
    are ones that ignore where the text was.
    """

    source: str
    target: str
    box: tuple[int, int, int, int] | None = None
    confidence: float = 0.0
    coverage: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "box": list(self.box) if self.box else None,
            "confidence": round(self.confidence, 4),
            "coverage": round(self.coverage, 4),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranslatedLine":
        raw_box = data.get("box")
        box: tuple[int, int, int, int] | None = None
        if isinstance(raw_box, (list, tuple)) and len(raw_box) == 4:
            box = (int(raw_box[0]), int(raw_box[1]), int(raw_box[2]), int(raw_box[3]))
        return cls(
            source=str(data.get("source", "")),
            target=str(data.get("target", "")),
            box=box,
            confidence=float(data.get("confidence", 0.0)),
            coverage=float(data.get("coverage", 1.0)),
        )


@dataclass
class OverlayUpdate:
    """One recognised + translated frame, ready for any surface to display."""

    source_text: str
    target_text: str
    coverage: float = 1.0
    confidence: float = 1.0
    latency_ms: float = 0.0
    trace: str = ""
    timestamp: float = field(default_factory=time.time)
    #: True when this text came back from the local model rather than the
    #: corpus/rules fast path (the two-tier display)
    refined: bool = False
    backend: str = ""
    #: Per-line detail with geometry, for layouts that need to know where the
    #: text was (in-place overlay, per-line blocks, per-line highlighting)
    lines: list[TranslatedLine] = field(default_factory=list)

    @property
    def is_new(self) -> bool:
        return bool(self.target_text and self.target_text != self.source_text)


@dataclass
class OverlayStats:
    """Live counters, shared by the overlay status line and the web dashboard."""

    fps: float = 0.0
    ocr_ms: float = 0.0
    translate_ms: float = 0.0
    total_ms: float = 0.0
    frames: int = 0
    skipped: int = 0
    corpus_entries: int = 0
    rules: int = 0
    cache_hit_rate: float = 0.0
    backend: str = ""
    paused: bool = False
    refinements: int = 0
    refinements_pending: int = 0
    refinements_dropped: int = 0
    memory_mib: float = 0.0


# --------------------------------------------------------------------------- #
# envelopes
# --------------------------------------------------------------------------- #


def encode_event(
    event_type: str,
    data: dict[str, Any] | None = None,
    seq: int | None = None,
    timestamp: float | None = None,
) -> dict[str, Any]:
    """Wrap a payload in the standard envelope."""
    if event_type not in ALL_EVENTS:
        raise ValueError(f"unknown event type {event_type!r}")
    envelope: dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "type": event_type,
        "ts": timestamp if timestamp is not None else time.time(),
        "data": data or {},
    }
    if seq is not None:
        envelope["seq"] = seq
    return envelope


def decode_envelope(envelope: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Validate and unwrap an envelope. Returns (type, data)."""
    if not isinstance(envelope, dict):
        raise ValueError("event must be a mapping")
    version = envelope.get("v")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schema version {version!r} (this build speaks {SCHEMA_VERSION})"
        )
    event_type = envelope.get("type")
    if event_type not in ALL_EVENTS:
        raise ValueError(f"unknown event type {event_type!r}")
    data = envelope.get("data") or {}
    if not isinstance(data, dict):
        raise ValueError("event data must be a mapping")
    return event_type, data


# --------------------------------------------------------------------------- #
# payload encoding
# --------------------------------------------------------------------------- #


def encode_update(update: OverlayUpdate) -> dict[str, Any]:
    return {
        "source": update.source_text,
        "target": update.target_text,
        "coverage": round(update.coverage, 4),
        "confidence": round(update.confidence, 4),
        "latency_ms": round(update.latency_ms, 2),
        "trace": update.trace,
        "refined": update.refined,
        "backend": update.backend,
        "lines": [line.as_dict() for line in update.lines],
    }


def decode_update(data: dict[str, Any]) -> OverlayUpdate:
    raw_lines = data.get("lines") or []
    lines = [
        TranslatedLine.from_dict(item)
        for item in raw_lines
        if isinstance(item, dict)
    ]
    return OverlayUpdate(
        source_text=str(data.get("source", "")),
        target_text=str(data.get("target", "")),
        coverage=float(data.get("coverage", 1.0)),
        confidence=float(data.get("confidence", 1.0)),
        latency_ms=float(data.get("latency_ms", 0.0)),
        trace=str(data.get("trace", "")),
        refined=bool(data.get("refined", False)),
        backend=str(data.get("backend", "")),
        lines=lines,
    )


def encode_stats(stats: OverlayStats) -> dict[str, Any]:
    return {
        "fps": round(stats.fps, 2),
        "ocr_ms": round(stats.ocr_ms, 2),
        "translate_ms": round(stats.translate_ms, 2),
        "total_ms": round(stats.total_ms, 2),
        "frames": stats.frames,
        "skipped": stats.skipped,
        "corpus_entries": stats.corpus_entries,
        "rules": stats.rules,
        "cache_hit_rate": round(stats.cache_hit_rate, 4),
        "backend": stats.backend,
        "paused": stats.paused,
        "refinements": stats.refinements,
        "refinements_pending": stats.refinements_pending,
        "refinements_dropped": stats.refinements_dropped,
        "memory_mib": round(stats.memory_mib, 1),
    }


def decode_stats(data: dict[str, Any]) -> OverlayStats:
    return OverlayStats(
        fps=float(data.get("fps", 0.0)),
        ocr_ms=float(data.get("ocr_ms", 0.0)),
        translate_ms=float(data.get("translate_ms", 0.0)),
        total_ms=float(data.get("total_ms", 0.0)),
        frames=int(data.get("frames", 0)),
        skipped=int(data.get("skipped", 0)),
        corpus_entries=int(data.get("corpus_entries", 0)),
        rules=int(data.get("rules", 0)),
        cache_hit_rate=float(data.get("cache_hit_rate", 0.0)),
        backend=str(data.get("backend", "")),
        paused=bool(data.get("paused", False)),
        refinements=int(data.get("refinements", 0)),
        refinements_pending=int(data.get("refinements_pending", 0)),
        refinements_dropped=int(data.get("refinements_dropped", 0)),
        memory_mib=float(data.get("memory_mib", 0.0)),
    )


def encode_refinement(refinement: Any) -> dict[str, Any]:
    """Encode a ``local_nmt.Refinement`` without importing it (avoids a cycle)."""
    return {
        "source": refinement.source_text,
        "target": refinement.target_text,
        "backend": refinement.backend,
        "protected_terms": int(refinement.protected_terms),
        "lost_placeholders": int(refinement.lost_placeholders),
        "elapsed_ms": round(refinement.elapsed_ms, 2),
        "used_model": bool(refinement.used_nmt),
        "fallback_reason": refinement.fallback_reason,
    }


def encode_trace_event(update: OverlayUpdate) -> dict[str, Any]:
    """The provenance line, for a diagnostics view."""
    return {
        "source": update.source_text,
        "target": update.target_text,
        "trace": update.trace,
        "refined": update.refined,
        "backend": update.backend,
    }


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def encode_command(name: str, **payload: Any) -> dict[str, Any]:
    if name not in ALL_COMMANDS:
        raise ValueError(f"unknown command {name!r}")
    body: dict[str, Any] = {"cmd": name}
    body.update(payload)
    return body


def decode_command(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("command must be a mapping")
    name = payload.get("cmd")
    if name not in ALL_COMMANDS:
        raise ValueError(f"unknown command {name!r}")
    return name, {k: v for k, v in payload.items() if k != "cmd"}


def command_result(name: str, ok: bool, detail: str = "", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"cmd": name, "ok": ok, "detail": detail}
    body.update(extra)
    return body
