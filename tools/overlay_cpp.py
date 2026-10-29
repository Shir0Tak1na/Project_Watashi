#!/usr/bin/env python3
"""Show the C++ engine's subtitles in the existing floating overlay.

This is the end-to-end surface for the native engine: it spawns
``watashi_cli.exe`` with ``--json-lines``, feeds every envelope through
``watashi.external_stream`` (decode only, no OCR, no model, no translation
logic of its own) and lets the normal ``Overlay`` paint the result.

    .venv\\Scripts\\python.exe tools\\overlay_cpp.py \\
        --capture screen --region 60,60,1200,200 --ocr onnx --duration 300

Everything the C++ side needs is passed through: ``--region``, ``--hwnd``,
``--window-title``, ``--fps``, ``--target``, corpus layers and ONNX model
paths. ``--duration`` is given to the engine, so the child stops itself and
this tool closes the overlay when the stream ends.

Exit codes: 0 ran and the engine exited normally, 1 the engine failed or the
adapter could not run, 2 bad arguments, 3 no desktop session (no Tk), which is
reported rather than passed off as a success.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

DEFAULT_CLI = ROOT / "build-w13" / "Release" / "watashi_cli.exe"
MODEL_DIR = ROOT / ".venv" / "Lib" / "site-packages" / "rapidocr_onnxruntime" / "models"


def _model(name: str) -> Path:
    return MODEL_DIR / name


def _default_corpus() -> list[str]:
    """The shipped dictionaries, labelled by the layer they belong to."""
    specs: list[str] = []
    for relative in ("rules/fiction", "rules/slang"):
        path = ROOT / relative
        if path.is_dir():
            specs.append(f"domain={path}")
    return specs


def build_command(args: argparse.Namespace) -> list[str]:
    command = [
        str(args.cli),
        "--capture", args.capture,
        "--ocr", args.ocr,
        "--translator", "corpus",
        "--json-lines",
        "--fps", str(args.fps),
        "--target", args.target,
        "--diff-threshold", str(args.diff_threshold),
    ]
    # The engine defaults to 2 s when --duration is absent; a tool duration of
    # 0 means "until Ctrl+C", so give it a day and stop it ourselves.
    command += ["--duration", str(args.duration if args.duration > 0 else 86400)]
    if args.region:
        command += ["--region", args.region]
    if args.hwnd:
        command += ["--hwnd", str(args.hwnd)]
    if args.window_title:
        command += ["--window-title", args.window_title]
        # the same title is useful to `when_window` corpus entries
        command += ["--window", args.window_title]
    if args.scene:
        command += ["--scene", args.scene]
    for spec in args.corpus:
        command += ["--corpus", spec]
    for path in args.rules:
        command += ["--rules", str(path)]
    if args.ocr == "onnx":
        command += [
            "--det", str(args.det),
            "--rec", str(args.rec),
            "--chars", str(args.chars),
        ]
        if args.cls:
            command += ["--cls", str(args.cls)]
    return command


def _physical_screen() -> tuple[int, int] | None:
    """Physical desktop size, for the overlay's DPI conversion, when knowable."""
    try:
        from watashi.capture import list_monitors

        monitors = list_monitors()
        if monitors:
            return (int(monitors[0]["width"]), int(monitors[0]["height"]))
    except Exception:
        return None
    return None


def _write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    parser.add_argument("--capture", choices=["screen", "window", "synthetic"], default="screen")
    parser.add_argument("--region", default=None, help="desktop rectangle x,y,w,h in physical pixels")
    parser.add_argument("--hwnd", type=int, default=0, help="window handle (window capture)")
    parser.add_argument("--window-title", default=None, help="window whose title contains this")
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--target", default="zh-CN")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="seconds to run; 0 runs until Ctrl+C")
    parser.add_argument("--diff-threshold", type=float, default=0.02)
    parser.add_argument("--ocr", choices=["onnx", "fake"], default="onnx")
    parser.add_argument("--det", type=Path, default=_model("ch_PP-OCRv4_det_infer.onnx"))
    parser.add_argument("--rec", type=Path, default=_model("ch_PP-OCRv4_rec_infer.onnx"))
    parser.add_argument("--cls", type=Path, default=_model("ch_ppocr_mobile_v2.0_cls_infer.onnx"))
    parser.add_argument("--chars", type=Path, default=ROOT / "spike" / "chars.txt")
    parser.add_argument("--corpus", action="append", default=None,
                        help="LAYER=PATH, repeatable; default: the shipped dictionaries")
    parser.add_argument("--rules", action="append", type=Path, default=[])
    parser.add_argument("--scene", default="")
    parser.add_argument("--presentation", default="bar")
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument("--max-restarts", type=int, default=3)
    parser.add_argument("--report", type=Path, default=None,
                        help="write a JSON report of the run here")
    parser.add_argument("--quiet", action="store_true", help="do not print adapter diagnostics")
    args = parser.parse_args(argv)

    if args.corpus is None:
        args.corpus = _default_corpus()
    if args.capture == "window" and not args.hwnd and not args.window_title:
        print("overlay_cpp: --capture window needs --hwnd or --window-title", file=sys.stderr)
        return 2
    if not args.cli.exists():
        print(f"overlay_cpp: engine binary not found: {args.cli}", file=sys.stderr)
        print("overlay_cpp: build it with  cmake --build build-w13 --config Release",
              file=sys.stderr)
        return 2
    if args.ocr == "onnx" and not (args.det.exists() and args.rec.exists() and args.chars.exists()):
        print("overlay_cpp: ONNX models or charset missing; pass --det --rec --chars "
              "or use --ocr fake", file=sys.stderr)
        return 2

    try:
        import tkinter as tk
        from tkinter import TclError
    except ImportError as exc:
        print(f"no desktop session: tkinter unavailable ({exc})", file=sys.stderr)
        return 3

    try:
        root = tk.Tk()
    except (TclError, RuntimeError, OSError) as exc:
        print(f"no desktop session: {exc}", file=sys.stderr)
        return 3
    root.withdraw()

    from watashi.external_stream import ExternalStream, OverlaySink
    from watashi.overlay import Overlay
    from watashi.presentation import PresentationSpec

    try:
        spec = PresentationSpec.preset(args.presentation)
    except Exception as exc:
        print(f"overlay_cpp: unknown presentation {args.presentation!r}: {exc}", file=sys.stderr)
        return 2

    overlay = Overlay(presentation=spec, physical_screen=_physical_screen())
    overlay.start(root=root)

    command = build_command(args)
    log = (lambda _text: None) if args.quiet else None
    sink = OverlaySink(overlay)
    stream = ExternalStream(
        command,
        sink,
        restart=not args.no_restart,
        max_restarts=args.max_restarts,
        cwd=str(ROOT),
        log=log,
    )
    reader = threading.Thread(target=stream.run, name="watashi-cpp-stream", daemon=True)
    reader.start()

    started = time.monotonic()
    # Safety net: the engine stops itself at --duration, but if it ever hangs
    # this closes the overlay a few seconds later instead of running forever.
    deadline = started + args.duration + 5.0 if args.duration > 0 else None
    finished = {"done": False}

    def finish() -> None:
        if finished["done"]:
            return
        finished["done"] = True
        if reader.is_alive():
            stream.stop()
        reader.join(timeout=2.0)
        report = stream.report
        elapsed = time.monotonic() - started
        payload = {
            "command": command,
            "elapsed_s": round(elapsed, 2),
            "stream": report.as_dict(),
            "overlay": {
                "renders": overlay.renders,
                "updates": sink.updates,
                "stats": sink.stats_count,
                "last_source": sink.last_update.source_text if sink.last_update else "",
                "last_target": sink.last_update.target_text if sink.last_update else "",
                "last_backend": sink.last_update.backend if sink.last_update else "",
                "last_status": sink.last_status,
                "translated": sink.translated,
                "distinct": [{"source": source, "target": target} for source, target in sink.distinct],
            },
        }
        if args.report:
            _write_report(args.report, payload)
            print(f"report: {args.report}", file=sys.stderr)
        last_code = report.exit_codes[-1] if report.exit_codes else None
        print(
            f"overlay: renders={overlay.renders} updates={sink.updates} stats={sink.stats_count}"
            f" | stream: lines={report.lines_read} events={report.events}"
            f" malformed={report.malformed} rejected={report.rejected}"
            f" gaps={report.seq_gaps} restarts={report.restarts}"
            f" exit={report.exit_codes}"
        )
        if sink.last_update is not None:
            print(f"last: {sink.last_update.source_text!r} -> {sink.last_update.target_text!r}")
        overlay.close(destroy_root=True)
        finished["exit_code"] = 0 if last_code == 0 else 1

    def poll() -> None:
        if not finished["done"]:
            if reader.is_alive():
                if deadline is not None and time.monotonic() >= deadline:
                    print("overlay_cpp: engine did not stop at --duration; stopping it",
                          file=sys.stderr)
                    stream.stop()
                try:
                    root.after(200, poll)
                except Exception:
                    finish()
                return
            finish()

    root.after(200, poll)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        finish()
    if not finished["done"]:
        finish()
    return int(finished.get("exit_code", 1))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
