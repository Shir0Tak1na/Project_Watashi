#!/usr/bin/env python3
"""Run every check that CI runs, on this machine, before pushing.

Why this exists: ``.github/workflows/checks.yml`` only executes where the repository
is, so a push is the only way to find out whether CI passes. This runs the same list --
``checks.txt``, the very file the workflow reads, so the two cannot disagree -- and
prints the same summaries, in about a minute:

    run.cmd check_all             the checks that need no display (what CI runs)
    run.cmd check_all --display   those plus the four that need one

Exit code is 0 only when every check passed, so it can be used as a gate.

One implementation for both platforms rather than a ``.cmd`` and a ``.sh``: the batch
version of this file was written first and parsed ``checks.txt`` wrongly -- ``delims=#``
skips *leading* delimiters, so every comment line contributed its first word as a script
name -- which is exactly the kind of mistake a second copy of a parser invites.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent


def names(list_file: Path) -> list[str]:
    """Check names from a list file: one per line, ``#`` comments and blanks ignored."""
    if not list_file.exists():
        return []
    found: list[str] = []
    for raw in list_file.read_text(encoding="utf-8").splitlines():
        name = raw.split("#")[0].strip()
        if name:
            found.append(name)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_all",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--display",
        action="store_true",
        help="also run the checks that need a display (they cannot run in CI)",
    )
    parser.add_argument(
        "--only", default="", help="comma separated names, to re-run a subset"
    )
    args = parser.parse_args(argv)

    selected = names(HERE / "checks.txt")
    if args.display:
        selected += names(HERE / "checks_display.txt")
    if args.only:
        wanted = {piece.strip() for piece in args.only.split(",") if piece.strip()}
        selected = [name for name in selected if name in wanted]
        missing = sorted(wanted - set(selected))
        if missing:
            print(f"[check_all] not in the lists: {', '.join(missing)}")
            return 2

    if not selected:
        print("[check_all] no checks found; is checks.txt missing?")
        return 2

    print("=" * 78, flush=True)
    print(f"Running {len(selected)} check(s) exactly as CI does", flush=True)
    print("=" * 78, flush=True)

    started = time.perf_counter()
    failed: list[str] = []
    for index, name in enumerate(selected, start=1):
        script = HERE / f"{name}.py"
        print("", flush=True)
        # Flushed, because each check inherits this stream: without it, redirecting the
        # output to a file puts every banner after all the check output, which reads
        # like the launcher ran nothing.
        print(
            f"--- [{index}/{len(selected)}] {name} " + "-" * max(0, 50 - len(name)),
            flush=True,
        )
        # Output is inherited rather than captured: the checks already print their own
        # summaries, and a failure is easier to read next to the check that produced it
        # than collected at the end. It also keeps this launcher free of pipes.
        result = subprocess.run(
            [sys.executable, str(script), "--summary"],
            cwd=str(REPO),
        )
        if result.returncode != 0:
            failed.append(name)

    elapsed = time.perf_counter() - started
    print("", flush=True)
    print("=" * 78, flush=True)
    print(
        f"  {len(selected) - len(failed)}/{len(selected)} check script(s) passed "
        f"in {elapsed:.0f}s",
        flush=True,
    )
    if failed:
        print(f"  failed: {', '.join(failed)}", flush=True)
    print("=" * 78, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
