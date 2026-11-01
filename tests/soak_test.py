"""Checks for tools/soak.ps1: argument validation, short runs and failure reports.

Every case uses the synthetic/fake path and reads only a JSON report. No model,
no network and no administrator rights are involved. The 10-minute smoke and the
8-hour soak are manual runs; this file keeps CTest short.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOAK = ROOT / "tools" / "soak.ps1"
POWERSHELL = "powershell.exe"


def log(message: str) -> None:
    print(message, flush=True)


def run(arguments: list[str], timeout: int = 240) -> subprocess.CompletedProcess:
    command = [
        POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(SOAK), *arguments,
    ]
    log("+ " + " ".join(command))
    result = subprocess.run(
        command, cwd=str(ROOT), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, timeout=timeout,
    )
    if result.stdout:
        print(result.stdout, flush=True)
    return result


def read_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def report_path(build: Path, name: str) -> Path:
    directory = build / "soak-test-reports"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def case_bad_args() -> None:
    for arguments in (
        ["--duration", "0"],
        ["--duration", "abc"],
        ["--mode", "nope"],
        ["--bogus"],
        ["--interval", "0"],
    ):
        result = run(arguments, timeout=60)
        if result.returncode != 2:
            raise SystemExit(
                f"soak.ps1 {arguments} returned {result.returncode}, expected 2"
            )
    log("soak.ps1 rejects bad duration, mode, interval and unknown options with 2")


def case_short_run(mode: str, duration: int, build: Path, config: str) -> None:
    report = report_path(build, f"soak-{mode}-{duration}s.json")
    if report.exists():
        report.unlink()
    result = run([
        "--duration", str(duration), "--mode", mode,
        "--report", str(report), "--build-dir", str(build),
        "--config", config, "--work-dir", str(ROOT),
    ])
    if result.returncode != 0:
        raise SystemExit(f"soak.ps1 {mode} run failed with {result.returncode}")
    if not report.exists():
        raise SystemExit(f"soak.ps1 did not write {report}")
    data = read_report(report)
    if data.get("pass") is not True:
        raise SystemExit(f"soak report is not passing: {data.get('failure_reason')}")
    if data.get("exit_code") != 0:
        raise SystemExit(f"soak report has exit code {data.get('exit_code')}")
    if data.get("requested_duration_seconds") != duration:
        raise SystemExit("soak report lost the requested duration")
    if data.get("mode") != mode:
        raise SystemExit("soak report lost the mode")
    if data.get("handles_start", 0) < 1:
        raise SystemExit("soak report has no starting handle count")
    if data.get("memory_mib_start", 0) <= 0:
        raise SystemExit("soak report has no starting working set")
    if data.get("log_errors") != 0:
        raise SystemExit("soak report counted log errors in a clean run")
    log(f"soak.ps1 {mode} smoke passed: handles={data['handles_start']} "
        f"memory={data['memory_mib_start']}MiB pass={data['pass']}")


def case_missing_exe(build: Path) -> None:
    report = report_path(build, "soak-missing-exe.json")
    result = run([
        "--duration", "1", "--exe", r"C:\definitely-missing\watashi_ui.exe",
        "--report", str(report),
    ], timeout=60)
    if result.returncode != 1:
        raise SystemExit(f"missing executable returned {result.returncode}, expected 1")
    data = read_report(report)
    if data.get("pass") is not False:
        raise SystemExit("missing executable was reported as passing")
    if "executable not found" not in data.get("failure_reason", ""):
        raise SystemExit("failure reason does not name the missing executable")
    log("soak.ps1 writes a failing report and exits 1 when the executable is missing")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True,
                        choices=["bad_args", "short_ui", "short_cli", "missing_exe"])
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--config", default="Release")
    arguments = parser.parse_args()

    if arguments.case == "bad_args":
        case_bad_args()
    elif arguments.case == "short_ui":
        case_short_run("ui", 3, arguments.build, arguments.config)
    elif arguments.case == "short_cli":
        case_short_run("cli", 3, arguments.build, arguments.config)
    elif arguments.case == "missing_exe":
        case_missing_exe(arguments.build)
    return 0


if __name__ == "__main__":
    sys.exit(main())
