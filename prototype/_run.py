#!/usr/bin/env python3
"""Dispatcher behind ``run.cmd`` / ``run.sh``.

Why this exists: on Windows, ``python`` on PATH is often the **Microsoft Store
app-execution alias**, a zero-byte reparse point. Running it opens a dialog
("application cannot be opened") or the Store instead of running Python, so any
documented command starting with a bare ``python`` is a trap.

The launchers resolve the project's own interpreter and call this file, so the
documented commands work regardless of what ``python`` means on the machine.

    run.cmd --selftest                 -> watashi_proto.py --selftest
    run.cmd fetch_model --check        -> fetch_model.py --check
    run.cmd bench_nmt --threads 2,4    -> bench_nmt.py --threads 2,4
    run.sh  --selftest                 -> the same, on Bash

Passing arguments through a launcher that ends in ``%*`` / ``"$@"`` keeps the
original quoting intact, which a batch ``shift`` loop would not.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

DEFAULT_SCRIPT = "watashi_proto.py"

#: Scripts the launcher can dispatch to, for the error message.
#:
#: Listed from the directory rather than hand-maintained: a hardcoded list goes
#: stale the moment a check is added, and then the error message actively
#: misleads -- it told the reader that ``selfcheck_desktop`` did not exist while
#: dispatching to it fine, and omitted ``--selftest`` entirely.
def available_scripts() -> list[str]:
    names = sorted(p.name for p in HERE.glob("*.py") if not p.name.startswith("_"))
    return names


def main(argv: list[str]) -> int:
    args = list(argv)

    # No argument, or the first one is a flag: default to the main CLI.
    if not args or args[0].startswith("-"):
        script = HERE / DEFAULT_SCRIPT
        rest = args
    else:
        name = args[0]
        if not name.endswith(".py"):
            name += ".py"
        script = HERE / name
        rest = args[1:]

    if not script.is_file():
        print(f"[run] no such script: {script.name}", file=sys.stderr)
        print("[run] available:", file=sys.stderr)
        for entry in available_scripts():
            print(f"         {entry[:-3]}", file=sys.stderr)
        print(
            f"[run] or pass no script name (or a flag) to run {DEFAULT_SCRIPT[:-3]}",
            file=sys.stderr,
        )
        return 2

    if not HERE.as_posix() in (p.replace("\\", "/") for p in sys.path):
        sys.path.insert(0, str(HERE))

    sys.argv = [str(script), *rest]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exit_request:
        code = exit_request.code
        if code is None:
            return 0
        return int(code) if isinstance(code, int) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
