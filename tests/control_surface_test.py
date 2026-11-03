"""Report-field checks for --control none/window using the synthetic path.

No model, no network and no real tray are involved. The UI needs a desktop, the
same as the other overlay tests. The control window's buttons are covered by
manual acceptance; this file proves the surface selection reaches the report.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = "watashi_ui.exe"


def log(message: str) -> None:
    print(message, flush=True)


def run_ui(build: Path, config: str, control: str, report: Path) -> subprocess.CompletedProcess:
    executable = build / config / UI
    command = [
        str(executable),
        "--capture", "synthetic", "--ocr", "fake",
        "--duration", "1", "--linger", "0",
        "--control", control,
        "--report", str(report),
    ]
    log("+ " + " ".join(command))
    result = subprocess.run(
        command, cwd=str(ROOT), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, timeout=120,
    )
    if result.stdout:
        print(result.stdout, flush=True)
    return result


def read_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def check(control: str, expected_surface: str, build: Path, config: str) -> None:
    report = build / f"control-{control}-report.json"
    if report.exists():
        report.unlink()
    result = run_ui(build, config, control, report)
    if result.returncode != 0:
        raise SystemExit(f"watashi_ui --control {control} exited {result.returncode}")
    if not report.exists():
        raise SystemExit(f"watashi_ui did not write {report}")
    data = read_report(report)
    surface = data.get("control_surface")
    if surface != expected_surface:
        raise SystemExit(f"--control {control}: surface {surface!r}, "
                         f"expected {expected_surface!r}")
    tray = data.get("tray", {})
    if tray.get("added") is not False:
        raise SystemExit(f"--control {control} unexpectedly added a tray icon")
    if data.get("exit_reason") != "duration":
        raise SystemExit(f"--control {control}: exit_reason {data.get('exit_reason')!r}")
    log(f"--control {control}: control_surface={surface} tray.added=false exit=duration")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, choices=["none", "window"])
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--config", default="Release")
    arguments = parser.parse_args()
    if arguments.case == "none":
        check("none", "hotkeys", arguments.build, arguments.config)
    else:
        check("window", "window", arguments.build, arguments.config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
