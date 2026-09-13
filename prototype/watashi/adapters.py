"""Adapters that connect a Session's event stream to a surface.

Each surface is an adapter over the same stream, so none of them owns engine
logic. In-process adapters live here; the HTTP/SSE one lives in ``web.py``.

===========================  ==============================================
surface                      adapter
===========================  ==============================================
floating overlay             ``OverlayAdapter``  (in-process queue)
CLI / PowerShell             ``ConsoleAdapter``  (text or JSON lines)
local web panel              ``web.py``          (SSE + POST)
===========================  ==============================================

Adapters run their own pump thread and hand off to whatever the surface uses
for thread safety (the overlay enqueues into its Tk pump; the console just
writes). Nothing here imports tkinter, so the engine can run headless.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from typing import Any, Callable, TextIO

from .events import (
    EVENT_ERROR,
    EVENT_PRESENTATION,
    EVENT_READY,
    EVENT_REFINEMENT,
    EVENT_STATS,
    EVENT_STATUS,
    EVENT_STOPPED,
    EVENT_SUBTITLE,
    OverlayStats,
    decode_stats,
    decode_update,
)
from .presentation import PresentationSpec


class _Pump:
    """Common start/stop plumbing for queue-draining adapters."""

    name = "adapter"

    def __init__(self, channel: "queue.Queue[dict[str, Any]]") -> None:
        self.channel = channel
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.handled = 0

    def handle(self, event: dict[str, Any]) -> None:
        """Consume one complete envelope.

        Adapters receive the whole envelope rather than a pre-split
        ``(type, data)`` pair, so a pass-through surface (``--json-lines``)
        cannot silently drop ``seq`` or ``ts`` on the way out.
        """
        raise NotImplementedError

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"watashi-{self.name}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                event = self.channel.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.handle(event)
            except Exception as exc:  # a display bug must not kill the engine
                print(f"[{self.name}] failed to handle event: {exc}", file=sys.stderr)
            self.handled += 1


class OverlayAdapter(_Pump):
    """Feeds a floating ``Overlay`` from the event stream.

    The overlay handles its own thread marshalling (it enqueues into the Tk
    pump), so this can simply call it from the adapter thread.
    """

    name = "overlay"

    def __init__(self, overlay: Any, channel: "queue.Queue[dict[str, Any]]") -> None:
        super().__init__(channel)
        self.overlay = overlay
        self.subtitles = 0
        self.refinements = 0
        self.last_stats: OverlayStats | None = None

    def handle(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "")
        data = event.get("data") or {}
        if event_type == EVENT_SUBTITLE:
            self.subtitles += 1
            self.overlay.push(decode_update(data))
        elif event_type == EVENT_REFINEMENT:
            self.refinements += 1
            self.overlay.push(_refinement_to_update(data))
        elif event_type == EVENT_STATS:
            self.last_stats = decode_stats(data)
            self.overlay.push_stats(self.last_stats)
        elif event_type == EVENT_STATUS:
            self.overlay.push_status(str(data.get("message", "")))
        elif event_type == EVENT_PRESENTATION:
            # another surface changed the look; keep this one in step
            try:
                self.overlay.apply_presentation(PresentationSpec.from_dict(data))
            except Exception as exc:
                print(f"[overlay] could not apply presentation: {exc}")
        elif event_type == EVENT_READY:
            box = data.get("region_box")
            if isinstance(box, (list, tuple)) and len(box) == 4:
                self.overlay.set_region_box(
                    (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
                )
        elif event_type == EVENT_ERROR:
            self.overlay.push_status(f"错误：{data.get('message', '')}")


class ConsoleAdapter(_Pump):
    """Prints the stream as human readable text or as JSON lines.

    ``--json-lines`` is the machine readable mode: one envelope per line, so a
    PowerShell or Bash pipeline can consume the engine without parsing prose.
    """

    name = "console"

    def __init__(
        self,
        channel: "queue.Queue[dict[str, Any]]",
        *,
        json_lines: bool = False,
        print_lines: bool = True,
        print_refined: bool = True,
        print_trace: bool = False,
        print_stats: bool = False,
        stream: TextIO | None = None,
    ) -> None:
        super().__init__(channel)
        self.json_lines = json_lines
        self.print_lines = print_lines
        self.print_refined = print_refined
        self.print_trace = print_trace
        self.print_stats = print_stats
        self.stream = stream or sys.stdout
        self._last_subtitle = ""
        self._last_refined = ""
        self.subtitles = 0
        self.refinements = 0

    def handle(self, event: dict[str, Any]) -> None:
        if self.json_lines:
            # pass the envelope through untouched: seq and ts are part of the
            # contract and a client may rely on ordering
            self._write(json.dumps(event, ensure_ascii=False))
            return

        event_type = event.get("type", "")
        data = event.get("data") or {}

        if event_type == EVENT_SUBTITLE and self.print_lines:
            target = str(data.get("target", ""))
            if target and target != self._last_subtitle:
                self._last_subtitle = target
                self.subtitles += 1
                latency = float(data.get("latency_ms", 0.0))
                self._write(f"  [{latency:6.0f} ms] {target}")
                if self.print_trace and data.get("trace"):
                    self._write(f"             {data['trace']}")

        elif event_type == EVENT_REFINEMENT and self.print_refined:
            target = str(data.get("target", ""))
            if target and target != self._last_refined:
                self._last_refined = target
                self.refinements += 1
                if data.get("used_model"):
                    origin = f"model {float(data.get('elapsed_ms', 0.0)):.0f}ms"
                    terms = int(data.get("protected_terms", 0))
                    if terms:
                        origin += f", {terms} term(s) protected"
                else:
                    origin = f"corpus ({data.get('fallback_reason')})"
                self._write(f"  [refined · {origin}] {target}")

        elif event_type == EVENT_STATS and self.print_stats:
            self._write(
                f"  [stats] fps={data.get('fps')} ocr={data.get('ocr_ms')}ms "
                f"total={data.get('total_ms')}ms mem={data.get('memory_mib')}MiB"
            )

        elif event_type == EVENT_ERROR:
            self._write(f"  [error] {data.get('message', '')}", stderr=True)

        elif event_type == EVENT_STATUS and self.print_stats:
            self._write(f"  [status] {data.get('message', '')}")

        elif event_type == EVENT_STOPPED:
            self._write("  [stopped]")

    def _write(self, text: str, stderr: bool = False) -> None:
        stream = sys.stderr if stderr else self.stream
        try:
            stream.write(text + "\n")
            stream.flush()
        except Exception:
            pass


class FanOut:
    """Runs several adapters over one subscription.

    A single subscription is shared so the session's queue limits (which drop
    stale display events for slow consumers) apply to the group as a whole.
    """

    def __init__(self, channel: "queue.Queue[dict[str, Any]]", adapters: list[_Pump]) -> None:
        self.channel = channel
        self.adapters = adapters

    def start(self) -> None:
        for adapter in self.adapters:
            adapter.start()

    def stop(self) -> None:
        for adapter in self.adapters:
            adapter.stop()

    def handle(self, event: dict[str, Any]) -> None:
        for adapter in self.adapters:
            adapter.handle(event)


def attach_console(
    channel: "queue.Queue[dict[str, Any]]", **kwargs: Any
) -> ConsoleAdapter:
    return ConsoleAdapter(channel, **kwargs)


def attach_overlay(overlay: Any, channel: "queue.Queue[dict[str, Any]]") -> OverlayAdapter:
    return OverlayAdapter(overlay, channel)


def _refinement_to_update(data: dict[str, Any]) -> Any:
    """Build an overlay update from a refinement payload."""
    from .events import OverlayUpdate

    terms = int(data.get("protected_terms", 0))
    trace = str(data.get("backend", ""))
    if data.get("fallback_reason"):
        trace += f" ({data['fallback_reason']})"
    if terms:
        trace += f" +{terms} terms"
    return OverlayUpdate(
        source_text=str(data.get("source", "")),
        target_text=str(data.get("target", "")),
        coverage=1.0,
        confidence=0.85,
        latency_ms=float(data.get("elapsed_ms", 0.0)),
        trace=trace,
        refined=True,
        backend=str(data.get("backend", "")),
    )
