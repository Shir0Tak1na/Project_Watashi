#!/usr/bin/env python3
"""Compare the C++ corpus engine with the recorded Python behaviour.

    .venv/Scripts/python.exe tools/compare_corpus.py \
        --cli build-w12/Release/watashi_cli.exe

For each case the CLI is asked to translate the same text with the same target,
scene and window, and the result is compared field by field: target, backend,
coverage and trace (the "reason" a line came out the way it did). Cases are
grouped by context so one model-free CLI invocation handles a whole group
instead of 120 process launches.

Exit 0 only when every case matches exactly.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "golden" / "corpus" / "cases.jsonl"
DEFAULT_CLI = ROOT / "build-w12" / "Release" / "watashi_cli.exe"
TOLERANCE = 1e-6


def read_cases(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def run_group(cli: Path, corpus: Path, cases: list[dict[str, Any]],
              target: str, scene: str, window: str) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="watashi-corpus-") as temporary:
        texts = Path(temporary) / "texts.txt"
        texts.write_text("\n".join(case["text"] for case in cases) + "\n", encoding="utf-8")
        command = [
            str(cli),
            "--translator", "corpus",
            "--corpus", f"user={corpus / 'data' / 'user'}",
            "--corpus", f"domain={corpus / 'data' / 'domain'}",
            "--corpus", f"general={corpus / 'data' / 'general'}",
            "--rules", str(corpus / "data" / "rules.json"),
            "--target", target,
            "--scene", scene,
            "--window", window,
            "--text-file", str(texts),
            "--json-lines",
        ]
        completed = subprocess.run(command, capture_output=True, text=True,
                                   encoding="utf-8")
    if completed.returncode != 0:
        raise SystemExit(f"cli exited {completed.returncode}:\n{completed.stderr.strip()}")
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    return [event["data"] for event in events if event.get("type") == "subtitle"]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--corpus", type=Path, default=CASES.parent)
    args = parser.parse_args(argv)

    if not args.cli.exists():
        raise SystemExit(f"missing {args.cli}; build first")

    cases = read_cases(args.cases)
    manifest = json.loads((args.corpus / "manifest.json").read_text(encoding="utf-8"))
    if manifest["cases"] != len(cases):
        raise SystemExit(f"manifest says {manifest['cases']} cases, file has {len(cases)}")

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for case in cases:
        groups.setdefault((case["target"], case["scene"], case["window"]), []).append(case)

    failures: list[str] = []
    checked = 0
    for (target, scene, window), group in groups.items():
        results = run_group(args.cli, args.corpus, group, target, scene, window)
        if len(results) != len(group):
            failures.append(
                f"context ({target!r}, {scene!r}, {window!r}): "
                f"{len(results)} subtitle event(s) for {len(group)} case(s)")
            continue
        for case, actual in zip(group, results):
            expected = case["expected"]
            checked += 1
            problems = []
            if actual.get("target") != expected["target"]:
                problems.append(f"target {actual.get('target')!r} != {expected['target']!r}")
            if actual.get("backend") != expected["backend"]:
                problems.append(f"backend {actual.get('backend')!r} != {expected['backend']!r}")
            coverage = float(actual.get("coverage", -1.0))
            if abs(coverage - expected["coverage"]) > TOLERANCE:
                problems.append(f"coverage {coverage} != {expected['coverage']}")
            if actual.get("trace") != expected["trace"]:
                problems.append(f"trace {actual.get('trace')!r} != {expected['trace']!r}")
            if problems:
                failures.append(
                    f"{case['id']} {case['text']!r} [{target}/{scene}/{window}]: "
                    + "; ".join(problems))

    print(f"cases: {checked}/{len(cases)} compared")
    print(f"groups: {len(groups)} CLI invocation(s)")
    if failures:
        print(f"FAIL {len(failures)} case(s) differ; first 10:")
        for line in failures[:10]:
            print(f"  - {line}")
        return 1
    print(f"PASS {checked}/{len(cases)} identical (target, backend, coverage, trace)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
