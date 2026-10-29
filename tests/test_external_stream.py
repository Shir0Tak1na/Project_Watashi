#!/usr/bin/env python3
"""Unit tests for the C++ JSONL transport adapter.

No display, no model and no OCR engine are needed: every case runs a scripted
Python child process that prints envelopes, so the adapter's behaviour is
asserted rather than hoped for.

    .venv\\Scripts\\python.exe tests\\test_external_stream.py

Also collectable by pytest (the cases are plain ``test_*`` functions).
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from watashi.events import OverlayStats, OverlayUpdate  # noqa: E402
from watashi.external_stream import (  # noqa: E402
    EventSink,
    ExternalStream,
    OverlaySink,
    StreamReport,
)


# --------------------------------------------------------------------------- #
# scripted producer
# --------------------------------------------------------------------------- #


def _envelope(event_type: str, data: dict, seq: int | None = None) -> dict:
    event = {"v": 1, "type": event_type, "ts": time.time(), "data": data}
    if seq is not None:
        event["seq"] = seq
    return event


def _child(
    chunks: list[object],
    *,
    exit_code: int = 0,
    stderr: str = "",
    delay: float = 0.0,
    tail: str = "",
) -> str:
    """Build a ``python -c`` script that prints ``chunks`` then exits.

    A dict is serialised as an envelope line; a string is printed verbatim, so
    malformed lines can be produced on purpose.
    """
    lines = []
    for chunk in chunks:
        if isinstance(chunk, str):
            lines.append(f"print({chunk!r}, flush=True)")
        else:
            lines.append(f"print(json.dumps({chunk!r}), flush=True)")
        if delay:
            lines.append(f"time.sleep({delay})")
    if stderr:
        lines.append(f"print({stderr!r}, file=sys.stderr, flush=True)")
    if tail:
        lines.append(tail)
    if exit_code:
        lines.append(f"sys.exit({exit_code})")
    preamble = "import json, sys, time\n"
    return preamble + "\n".join(lines)


def _run(script: str, sink: EventSink, **kwargs) -> StreamReport:
    stream = ExternalStream(
        [sys.executable, "-c", script],
        sink,
        log=lambda _text: None,
        **kwargs,
    )
    return stream.run()


# --------------------------------------------------------------------------- #
# recorder
# --------------------------------------------------------------------------- #


class Recorder:
    def __init__(self, delay: float = 0.0) -> None:
        self.updates: list[OverlayUpdate] = []
        self.stats: list[OverlayStats] = []
        self.statuses: list[str] = []
        self.ready: list[dict] = []
        self.stopped: list[str] = []
        self.delay = delay

    def on_ready(self, data: dict) -> None:
        self.ready.append(data)

    def on_update(self, update: OverlayUpdate) -> None:
        if self.delay:
            time.sleep(self.delay)
        self.updates.append(update)

    def on_stats(self, stats: OverlayStats) -> None:
        self.stats.append(stats)

    def on_status(self, text: str) -> None:
        self.statuses.append(text)

    def on_stopped(self, reason: str) -> None:
        self.stopped.append(reason)


# --------------------------------------------------------------------------- #
# decoding and stream health
# --------------------------------------------------------------------------- #


def test_stream_decodes_every_event_type() -> None:
    chunks = [
        _envelope("ready", {"region_box": [60, 60, 1260, 260], "mode": "screen"}, 1),
        _envelope(
            "subtitle",
            {
                "source": "the sword intent of this sect is a myth",
                "target": "此宗剑意只是传说",
                "coverage": 0.5,
                "confidence": 0.9,
                "latency_ms": 42.0,
                "backend": "corpus",
                "lines": [
                    {
                        "source": "the sword intent",
                        "target": "剑意",
                        "box": [10, 20, 300, 40],
                        "confidence": 0.9,
                        "coverage": 0.5,
                    }
                ],
            },
            2,
        ),
        _envelope("stats", {"fps": 10.0, "frames": 7, "ocr_ms": 33.0, "mode": "screen"}, 3),
        _envelope("status", {"message": "corpus loaded"}, 4),
        _envelope("stopped", {"reason": "duration", "frames": 7}, 5),
    ]
    sink = Recorder()
    report = _run(_child(chunks), sink)

    assert len(sink.ready) == 1
    assert sink.ready[0]["region_box"] == [60, 60, 1260, 260]
    assert len(sink.updates) == 1
    update = sink.updates[0]
    assert update.source_text == "the sword intent of this sect is a myth"
    assert update.target_text == "此宗剑意只是传说"
    assert update.backend == "corpus"
    assert update.latency_ms == 42.0
    assert len(update.lines) == 1
    assert update.lines[0].box == (10, 20, 300, 40)
    assert len(sink.stats) == 1
    assert sink.stats[0].frames == 7
    assert sink.stats[0].mode == "screen"
    assert sink.statuses == ["corpus loaded"]
    assert sink.stopped == ["duration"]
    assert report.events == 5
    assert report.lines_read == 5
    assert report.exit_codes == [0]
    assert report.stopped_reason == "duration"
    assert report.clean


def test_malformed_json_is_counted_and_the_stream_continues() -> None:
    chunks = [
        "{not json at all",
        _envelope("subtitle", {"source": "a", "target": "甲"}, 2),
    ]
    sink = Recorder()
    report = _run(_child(chunks), sink)

    assert report.malformed == 1
    assert report.rejected == 0
    assert len(sink.updates) == 1
    assert not report.clean
    assert report.samples and "invalid JSON" in report.samples[0]


def test_bad_envelope_version_and_unknown_type_are_rejected() -> None:
    chunks = [
        {"v": 2, "type": "subtitle", "ts": 1.0, "seq": 1, "data": {}},
        {"v": 1, "type": "caption", "ts": 1.0, "seq": 2, "data": {}},
        _envelope("subtitle", {"source": "b", "target": "乙"}, 3),
    ]
    sink = Recorder()
    report = _run(_child(chunks), sink)

    assert report.rejected == 2
    assert report.malformed == 0
    assert len(sink.updates) == 1


def test_blank_lines_are_ignored_not_malformed() -> None:
    chunks = ["", _envelope("subtitle", {"source": "c", "target": "丙"}, 1)]
    sink = Recorder()
    report = _run(_child(chunks), sink)

    assert report.lines_read == 1
    assert report.malformed == 0
    assert len(sink.updates) == 1


def test_a_sequence_gap_is_counted_and_the_event_is_still_delivered() -> None:
    chunks = [
        _envelope("subtitle", {"source": "d1", "target": "丁1"}, 1),
        _envelope("subtitle", {"source": "d3", "target": "丁3"}, 3),
    ]
    sink = Recorder()
    report = _run(_child(chunks), sink)

    assert len(sink.updates) == 2
    assert report.seq_gaps == 1
    assert report.seq_missing == 1
    assert not report.clean


def test_a_sequence_regression_is_counted() -> None:
    chunks = [
        _envelope("subtitle", {"source": "e1", "target": "戊1"}, 1),
        _envelope("subtitle", {"source": "e2", "target": "戊2"}, 2),
        _envelope("subtitle", {"source": "e3", "target": "戊3"}, 1),
    ]
    sink = Recorder()
    report = _run(_child(chunks), sink)

    assert len(sink.updates) == 3
    assert report.seq_regressions == 1
    assert report.seq_gaps == 0


def test_missing_sequence_numbers_are_counted() -> None:
    chunks = [
        {"v": 1, "type": "subtitle", "ts": 1.0, "data": {"source": "f", "target": "己"}},
    ]
    sink = Recorder()
    report = _run(_child(chunks), sink)

    assert len(sink.updates) == 1
    assert report.missing_seq == 1


# --------------------------------------------------------------------------- #
# process lifecycle
# --------------------------------------------------------------------------- #


def test_nonzero_exit_is_recorded_and_restarted() -> None:
    with tempfile.TemporaryDirectory() as directory:
        marker = Path(directory) / "first-run-done"
        script = _child(
            [_envelope("subtitle", {"source": "g", "target": "庚"}, 1)],
            tail=(
                "import os.path\n"
                f"marker = {str(marker)!r}\n"
                "if not os.path.exists(marker):\n"
                "    open(marker, 'w').close()\n"
                "    sys.exit(2)\n"
                "sys.exit(0)\n"
            ),
        )
        sink = Recorder()
        report = _run(script, sink, restart=True, max_restarts=1, restart_delay=0.05)

    assert report.restarts == 1
    assert report.exit_codes == [2, 0]
    assert len(sink.updates) == 2


def test_nonzero_exit_without_restart_stops_immediately() -> None:
    script = _child(
        [_envelope("subtitle", {"source": "h", "target": "辛"}, 1)],
        stderr="engine exploded",
        exit_code=3,
    )
    sink = Recorder()
    report = _run(script, sink, restart=False)

    assert report.exit_codes == [3]
    assert report.restarts == 0
    assert report.last_error == "engine exploded"
    assert not report.clean


def test_restart_is_bounded_by_max_restarts() -> None:
    script = _child(
        [_envelope("subtitle", {"source": "i", "target": "壬"}, 1)],
        stderr="still failing",
        exit_code=2,
    )
    sink = Recorder()
    report = _run(script, sink, restart=True, max_restarts=2, restart_delay=0.01)

    assert report.restarts == 2
    assert report.exit_codes == [2, 2, 2]
    assert len(sink.updates) == 3


def test_restart_starts_a_new_sequence_epoch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        marker = Path(directory) / "first-run-done"
        first = [
            _envelope("subtitle", {"source": "j1", "target": "癸1"}, 1),
            _envelope("subtitle", {"source": "j2", "target": "癸2"}, 2),
        ]
        script = (
            "import json, sys, time, os.path\n"
            f"marker = {str(marker)!r}\n"
            "if not os.path.exists(marker):\n"
            "    open(marker, 'w').close()\n"
            "    events = " + repr(first) + "\n"
            "    for event in events:\n"
            "        print(json.dumps(event), flush=True)\n"
            "    sys.exit(2)\n"
            "print(json.dumps(" + repr(_envelope("subtitle", {"source": "j1", "target": "癸1"}, 1)) +
            "), flush=True)\n"
            "sys.exit(0)\n"
        )
        sink = Recorder()
        report = _run(script, sink, restart=True, max_restarts=1, restart_delay=0.05)

    assert report.restarts == 1
    # seq 1 again after a restart must not be called a regression
    assert report.seq_regressions == 0
    assert len(sink.updates) == 3


def test_stderr_tail_is_kept_for_diagnostics() -> None:
    script = _child(
        [_envelope("subtitle", {"source": "k", "target": "子"}, 1)],
        stderr="line one\nline two\nengine died",
        exit_code=4,
    )
    sink = Recorder()
    report = _run(script, sink, restart=False)

    assert report.exit_codes == [4]
    assert "engine died" in report.last_error


def test_stop_terminates_a_hung_child() -> None:
    script = _child(
        [_envelope("subtitle", {"source": "l", "target": "丑"}, 1)],
        tail="time.sleep(30)",
    )
    sink = Recorder()
    stream = ExternalStream(
        [sys.executable, "-c", script], sink, log=lambda _text: None, restart=False
    )
    thread = threading.Thread(target=stream.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5.0
    while not sink.updates and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sink.updates, "the child never produced its first event"

    started = time.monotonic()
    stream.stop()
    thread.join(timeout=3.0)
    assert not thread.is_alive()
    assert time.monotonic() - started < 3.0


def test_a_slow_sink_throttles_without_dropping_lines() -> None:
    count = 2000
    # Generated inside the child: 2000 envelopes in a command line would pass
    # the Windows 32k limit and fail for the wrong reason.
    script = (
        "import json\n"
        "count = " + str(count) + "\n"
        "for index in range(count):\n"
        "    event = {'v': 1, 'type': 'subtitle', 'ts': 0.0, 'seq': index + 1,\n"
        "             'data': {'source': 's' + str(index), 'target': '寅'}}\n"
        "    print(json.dumps(event), flush=True)\n"
    )
    sink = Recorder(delay=0.0002)
    started = time.monotonic()
    report = _run(script, sink, restart=False)
    elapsed = time.monotonic() - started

    assert len(sink.updates) == count
    assert report.lines_read == count
    assert report.malformed == 0
    assert elapsed >= 0.2, "the sink's sleeps should dominate; nothing may be buffered away"


# --------------------------------------------------------------------------- #
# overlay sink and the tool's no-display path
# --------------------------------------------------------------------------- #


class FakeOverlay:
    def __init__(self) -> None:
        self.updates: list[OverlayUpdate] = []
        self.stats: list[OverlayStats] = []
        self.statuses: list[str] = []
        self.region_box = None

    def push(self, update: OverlayUpdate) -> None:
        self.updates.append(update)

    def push_stats(self, stats: OverlayStats) -> None:
        self.stats.append(stats)

    def push_status(self, text: str) -> None:
        self.statuses.append(text)

    def set_region_box(self, box) -> None:
        self.region_box = box


def test_overlay_sink_maps_the_stream_onto_overlay_calls() -> None:
    chunks = [
        _envelope("ready", {"region_box": [60, 60, 1260, 260]}, 1),
        _envelope("subtitle", {"source": "m", "target": "卯"}, 2),
        _envelope("stats", {"frames": 1}, 3),
        _envelope("stopped", {"reason": "duration"}, 4),
    ]
    overlay = FakeOverlay()
    sink = OverlaySink(overlay)
    report = _run(_child(chunks), sink)

    assert overlay.region_box == (60, 60, 1260, 260)
    assert len(overlay.updates) == 1
    assert overlay.updates[0].target_text == "卯"
    assert len(overlay.stats) == 1
    assert overlay.statuses and "停止" in overlay.statuses[-1]
    assert sink.updates == 1
    assert sink.stats_count == 1
    assert sink.last_update is not None
    assert report.events == 4


def test_overlay_tool_exits_3_without_a_display() -> None:
    import importlib.util
    import types

    sys.path.insert(0, str(ROOT / "tools"))
    spec = importlib.util.spec_from_file_location(
        "overlay_cpp_tool", ROOT / "tools" / "overlay_cpp.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fake = types.ModuleType("tkinter")

    class TclError(Exception):
        pass

    def no_display():
        raise TclError("no display name and no $DISPLAY environment variable")

    fake.TclError = TclError
    fake.Tk = no_display
    saved = sys.modules.get("tkinter")
    sys.modules["tkinter"] = fake
    try:
        code = module.main(["--ocr", "fake", "--duration", "0.1"])
    finally:
        if saved is None:
            sys.modules.pop("tkinter", None)
        else:
            sys.modules["tkinter"] = saved

    assert code == 3


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #


def main() -> int:
    cases = [
        (name, value)
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failed = 0
    for name, case in cases:
        try:
            case()
        except Exception as exc:  # noqa: BLE001 - a failing case must not stop the rest
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
        else:
            print(f"PASS {name}")
    print(f"{len(cases) - failed}/{len(cases)} adapter test(s) passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
