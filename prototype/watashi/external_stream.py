"""Drive the existing overlay from the C++ engine's JSONL stream.

The C++ CLI already publishes the same envelope the Python engine does
(``{"v", "type", "ts", "seq", "data"}``), so no translation logic belongs
here. This module is only transport and adaptation:

* **spawn** the C++ process and read its stdout a line at a time;
* **decode** each envelope with ``events.decode_envelope`` and the payload with
  ``events.decode_update`` / ``decode_stats`` into the existing dataclasses;
* **hand** them to a sink (the floating ``Overlay`` in production, a recorder
  in tests).

Nothing here imports an OCR engine, a model, tkinter or a translator. A
sink bug must never take the reader down, malformed lines are counted and
skipped, and a non-zero child exit is reported rather than guessed at.

Backpressure is the OS pipe: the reader consumes synchronously, so if the sink
is slow the pipe fills and the child blocks on ``write`` instead of the parent
accumulating an unbounded list. The overlay's own queue additionally drops
stale display frames, which is its documented behaviour, not ours.

Sequence numbers are checked, not repaired: a gap or a regression is counted
in the report, and the event is still delivered. A restart starts a new epoch,
so the second process starting at ``seq`` 1 is not a regression.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from .events import (
    EVENT_ERROR,
    EVENT_READY,
    EVENT_STATS,
    EVENT_STATUS,
    EVENT_STOPPED,
    EVENT_SUBTITLE,
    OverlayStats,
    OverlayUpdate,
    decode_envelope,
    decode_stats,
    decode_update,
)

__all__ = [
    "EventSink",
    "NullSink",
    "OverlaySink",
    "StreamReport",
    "ExternalStream",
]


class EventSink(Protocol):
    """What a stream consumer implements. The overlay sink is one of these."""

    def on_ready(self, data: dict[str, Any]) -> None: ...

    def on_update(self, update: OverlayUpdate) -> None: ...

    def on_stats(self, stats: OverlayStats) -> None: ...

    def on_status(self, text: str) -> None: ...

    def on_stopped(self, reason: str) -> None: ...


class NullSink:
    """Consumes nothing. Useful for a health check on the stream itself."""

    def on_ready(self, data: dict[str, Any]) -> None:
        pass

    def on_update(self, update: OverlayUpdate) -> None:
        pass

    def on_stats(self, stats: OverlayStats) -> None:
        pass

    def on_status(self, text: str) -> None:
        pass

    def on_stopped(self, reason: str) -> None:
        pass


class OverlaySink:
    """Adapts the stream to the methods an ``Overlay`` already exposes.

    Every call lands on an overlay method that is safe from another thread:
    ``push``/``push_stats``/``push_status`` enqueue into the Tk pump, and
    ``set_region_box`` does the same. The last update is kept so a report can
    say what was actually shown without reading private overlay state.
    """

    def __init__(self, overlay: Any) -> None:
        self.overlay = overlay
        self.updates = 0
        self.stats_count = 0
        self.last_update: OverlayUpdate | None = None
        self.last_stats: OverlayStats | None = None
        self.last_status = ""
        self.last_ready: dict[str, Any] = {}
        #: distinct source -> target pairs actually pushed, capped for reports
        self.distinct: list[tuple[str, str]] = []
        #: updates whose target differs from the source (a real translation)
        self.translated = 0

    def on_ready(self, data: dict[str, Any]) -> None:
        self.last_ready = dict(data)
        box = data.get("region_box")
        if isinstance(box, (list, tuple)) and len(box) == 4:
            try:
                self.overlay.set_region_box(
                    (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
                )
            except Exception as exc:  # a bad box must not stop the reader
                print(f"[external-stream] bad region_box: {exc}", file=sys.stderr)

    def on_update(self, update: OverlayUpdate) -> None:
        self.updates += 1
        self.last_update = update
        pair = (update.source_text.strip(), update.target_text.strip())
        if pair[1] and pair[1] != pair[0]:
            self.translated += 1
        if pair[1] and pair not in self.distinct and len(self.distinct) < 12:
            self.distinct.append(pair)
        self.overlay.push(update)

    def on_stats(self, stats: OverlayStats) -> None:
        self.stats_count += 1
        self.last_stats = stats
        self.overlay.push_stats(stats)

    def on_status(self, text: str) -> None:
        self.last_status = text
        self.overlay.push_status(text)

    def on_stopped(self, reason: str) -> None:
        self.last_status = f"引擎已停止（{reason}）" if reason else "引擎已停止"
        self.overlay.push_status(self.last_status)


@dataclass
class StreamReport:
    """Everything the adapter observed, including what it refused."""

    lines_read: int = 0
    events: int = 0
    malformed: int = 0
    rejected: int = 0
    missing_seq: int = 0
    seq_gaps: int = 0
    seq_missing: int = 0
    seq_regressions: int = 0
    restarts: int = 0
    exit_codes: list[int] = field(default_factory=list)
    stopped_reason: str = ""
    last_error: str = ""
    duration_s: float = 0.0
    samples: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True when nothing had to be repaired and the child exited normally."""
        return (
            self.malformed == 0
            and self.rejected == 0
            and self.seq_gaps == 0
            and self.seq_regressions == 0
            and (not self.exit_codes or self.exit_codes[-1] == 0)
        )

    def as_dict(self) -> dict[str, Any]:
        data = {
            "lines_read": self.lines_read,
            "events": self.events,
            "malformed": self.malformed,
            "rejected": self.rejected,
            "missing_seq": self.missing_seq,
            "seq_gaps": self.seq_gaps,
            "seq_missing": self.seq_missing,
            "seq_regressions": self.seq_regressions,
            "restarts": self.restarts,
            "exit_codes": list(self.exit_codes),
            "stopped_reason": self.stopped_reason,
            "last_error": self.last_error,
            "duration_s": round(self.duration_s, 3),
            "clean": self.clean,
        }
        if self.samples:
            data["samples"] = list(self.samples)
        return data


class ExternalStream:
    """Runs one child process's JSONL stream into a sink, restarting on failure.

    ``command`` is injected so the adapter can be tested against a scripted
    producer instead of the C++ binary; the production caller passes
    ``[watashi_cli.exe, "--capture", "screen", ..., "--json-lines"]``.
    """

    def __init__(
        self,
        command: Sequence[str],
        sink: EventSink,
        *,
        restart: bool = True,
        max_restarts: int = 3,
        restart_delay: float = 0.5,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stderr_tail: int = 16,
        sample_limit: int = 3,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.command = list(command)
        self.sink = sink
        self.restart = restart
        self.max_restarts = max(0, int(max_restarts))
        self.restart_delay = max(0.0, float(restart_delay))
        self.cwd = cwd
        self.env = env
        self.stderr_tail = max(1, int(stderr_tail))
        self.sample_limit = max(0, int(sample_limit))
        self._log = log or (lambda text: print(text, file=sys.stderr))

        self.report = StreamReport()
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._stderr_lines: list[str] = []
        self._last_seq: int | None = None
        self._stopped_seen = False
        self._running = False
        self._start_time = 0.0

    # -- state ------------------------------------------------------------- #

    @property
    def process(self) -> subprocess.Popen[str] | None:
        return self._process

    @property
    def running(self) -> bool:
        return self._running and not self._finished.is_set()

    @property
    def stderr_lines(self) -> list[str]:
        with self._lock:
            return list(self._stderr_lines)

    # -- lifecycle --------------------------------------------------------- #

    def run(self) -> StreamReport:
        """Read until the child finishes, the stream stops, or restarts run out."""
        if self._running:
            raise RuntimeError("ExternalStream is already running")
        self._running = True
        self._start_time = time.monotonic()
        try:
            while True:
                if self._stop.is_set():
                    break
                exit_code = self._run_once()
                self.report.exit_codes.append(exit_code)
                if self._stop.is_set() or self._stopped_seen or exit_code == 0:
                    break
                if not self.restart or self.report.restarts >= self.max_restarts:
                    break
                self.report.restarts += 1
                self._log(
                    f"[external-stream] engine exited {exit_code}; restarting "
                    f"({self.report.restarts}/{self.max_restarts})"
                )
                if self.restart_delay and self._stop.wait(self.restart_delay):
                    break
        finally:
            self.report.duration_s = time.monotonic() - self._start_time
            if not self._stop.is_set() and self.report.exit_codes:
                if self.report.exit_codes[-1] != 0 and self._last_stderr():
                    self.report.last_error = self._last_stderr()
            self._finished.set()
            self._running = False
        return self.report

    def stop(self, timeout: float = 2.0) -> None:
        """Ask the reader to finish and make sure the child is gone."""
        self._stop.set()
        self._terminate(timeout)
        self._finished.wait(timeout + 1.0)

    def _terminate(self, timeout: float = 2.0) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass

    # -- one process ------------------------------------------------------- #

    def _run_once(self) -> int:
        self._last_seq = None
        self._stopped_seen = False
        with self._lock:
            self._stderr_lines.clear()
        try:
            process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self.cwd,
                env=self.env,
            )
        except OSError as exc:
            self.report.last_error = f"cannot start the engine: {exc}"
            self._log(f"[external-stream] {self.report.last_error}")
            return 127

        self._process = process
        drain = threading.Thread(
            target=self._drain_stderr, args=(process,), name="watashi-stderr", daemon=True
        )
        drain.start()

        try:
            assert process.stdout is not None
            for raw in process.stdout:
                if self._stop.is_set():
                    break
                self._handle_line(raw)
        finally:
            try:
                process.stdout.close()
            except Exception:
                pass

        code = process.wait()
        drain.join(timeout=1.0)
        self._process = None
        if code != 0 and self._last_stderr():
            self.report.last_error = self._last_stderr()
        return int(code)

    def _drain_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for raw in process.stderr:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            with self._lock:
                self._stderr_lines.append(line)
                if len(self._stderr_lines) > self.stderr_tail:
                    del self._stderr_lines[: -self.stderr_tail]

    def _last_stderr(self) -> str:
        with self._lock:
            return self._stderr_lines[-1] if self._stderr_lines else ""

    # -- one line ---------------------------------------------------------- #

    def _handle_line(self, raw: str) -> None:
        line = raw.strip()
        if not line:
            return
        self.report.lines_read += 1
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError as exc:
            self._bad(line, f"invalid JSON: {exc.msg}")
            return
        if not isinstance(envelope, dict):
            self._bad(line, "event is not a JSON object")
            return
        try:
            event_type, data = decode_envelope(envelope)
        except ValueError as exc:
            self.report.rejected += 1
            self._sample(line, str(exc))
            return

        self._check_seq(envelope.get("seq"))
        self.report.events += 1
        try:
            self._dispatch(event_type, data)
        except Exception as exc:  # a sink failure must not stop the stream
            self._log(f"[external-stream] sink failed on {event_type}: {exc}")

    def _bad(self, line: str, why: str) -> None:
        self.report.malformed += 1
        self._sample(line, why)

    def _sample(self, line: str, why: str) -> None:
        if self.sample_limit and len(self.report.samples) < self.sample_limit:
            self.report.samples.append(f"{why}: {line[:160]}")
        self._log(f"[external-stream] {why}: {line[:160]}")

    def _check_seq(self, seq: Any) -> None:
        if seq is None:
            self.report.missing_seq += 1
            return
        if not isinstance(seq, int) or isinstance(seq, bool):
            self.report.missing_seq += 1
            return
        previous = self._last_seq
        if previous is not None:
            if seq <= previous:
                self.report.seq_regressions += 1
            elif seq > previous + 1:
                self.report.seq_gaps += 1
                self.report.seq_missing += seq - previous - 1
        if previous is None or seq > previous:
            self._last_seq = seq

    def _dispatch(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type == EVENT_SUBTITLE:
            self.sink.on_update(decode_update(data))
        elif event_type == EVENT_STATS:
            self.sink.on_stats(decode_stats(data))
        elif event_type == EVENT_READY:
            self.sink.on_ready(data)
        elif event_type == EVENT_STATUS:
            self.sink.on_status(str(data.get("message", "")))
        elif event_type == EVENT_ERROR:
            self.sink.on_status(f"错误：{data.get('message', '')}")
        elif event_type == EVENT_STOPPED:
            self._stopped_seen = True
            self.report.stopped_reason = str(data.get("reason", ""))
            self.sink.on_stopped(self.report.stopped_reason)
        # other C++ event types are not produced today; ignoring them keeps a
        # future addition from breaking this reader
