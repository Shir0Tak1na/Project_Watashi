#!/usr/bin/env python3
"""Read watashi_ui.exe's import table and prove the overlay links no engine.

The overlay spawns the CLI and talks to it over the event contract, so its EXE
must not pull in ONNX Runtime, CTranslate2, oneDNN or SentencePiece. A DLL
sitting beside the binary is fine; an import is not.

dumpbin is not on PATH in a normal shell, so it is located through vswhere.
Exit 77 (ctest: skipped) when neither dumpbin nor the binary is available; this
check is about the link line, and a machine without the toolchain cannot see it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

FORBIDDEN = ("onnxruntime", "ctranslate2", "dnnl", "sentencepiece")


def find_dumpbin() -> Path | None:
    on_path = shutil.which("dumpbin")
    if on_path:
        return Path(on_path)
    program_files = os.environ.get("ProgramFiles(x86)") or os.environ.get("ProgramFiles")
    if not program_files:
        return None
    vswhere = Path(program_files) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        return None
    result = subprocess.run(
        [str(vswhere), "-latest", "-products", "*",
         "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
         "-property", "installationPath"],
        capture_output=True, text=True, timeout=60)
    install = result.stdout.strip().splitlines()
    if not install:
        return None
    candidates = sorted(
        (Path(install[0]) / "VC" / "Tools" / "MSVC").glob("*/bin/Hostx64/x64/dumpbin.exe"))
    return candidates[-1] if candidates else None


def find_ui(build: Path, config: str) -> Path | None:
    for candidate in (build / config / "watashi_ui.exe", build / "watashi_ui.exe"):
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--config", default="Release")
    args = parser.parse_args()

    ui = find_ui(args.build, args.config)
    dumpbin = find_dumpbin()
    if ui is None or dumpbin is None:
        missing = "watashi_ui.exe" if ui is None else "dumpbin"
        print(f"ui_dependency: SKIP {missing} not available", file=sys.stderr)
        return 77

    result = subprocess.run([str(dumpbin), "/nologo", "/dependents", str(ui)],
                            capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(result.stdout, result.stderr, file=sys.stderr)
        return 1

    imports = result.stdout.lower()
    offenders = [name for name in FORBIDDEN if name in imports]
    if offenders:
        print(f"ui_dependency: {ui.name} imports {', '.join(offenders)}", file=sys.stderr)
        print(result.stdout, file=sys.stderr)
        return 1

    print(f"ui_dependency: {ui.name} imports no {', '.join(FORBIDDEN)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
