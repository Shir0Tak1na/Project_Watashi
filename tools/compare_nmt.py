#!/usr/bin/env python3
"""Compare the C++ NMT backend against the recorded Python reference.

    .venv/Scripts/python.exe tools/compare_nmt.py --cli build-w2/Release/watashi_cli.exe

For every case in ``golden/nmt/cases.jsonl`` this compares, field for field:

    target, backend, protected_terms, lost_placeholders, fallback_reason,
    used_model

Cases are grouped by (target language, scene, window) so the CLI loads the
model once per group instead of once per case, and each line is still translated
individually -- exactly the batch-of-one the reference was recorded with.
Exit 0 only when every field of every case matches; the first ten failures are
printed with the differing field.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLI = ROOT / "build-w2" / "Release" / "watashi_cli.exe"
DEFAULT_CASES = ROOT / "golden" / "nmt" / "cases.jsonl"

COMPARE_FIELDS = (
    "target",
    "backend",
    "protected_terms",
    "lost_placeholders",
    "fallback_reason",
    "used_model",
)


def load_cases(path: Path) -> list[dict]:
    cases = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            raise SystemExit(f"{path}:{number}: blank line")
        case = json.loads(raw)
        for field in ("id", "source", "target_lang", "scene", "window", "expected"):
            if field not in case:
                raise SystemExit(f"{path}:{number}: missing {field!r}")
        cases.append(case)
    return cases


def group_cases(cases: list[dict]) -> list[tuple[tuple[str, str, str], list[dict]]]:
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for case in cases:
        key = (case["target_lang"], case["scene"], case["window"])
        groups.setdefault(key, []).append(case)
    return list(groups.items())


def cli_command(cli: Path, manifest: dict, key: tuple[str, str, str], text_file: Path) -> list[str]:
    target, scene, window = key
    corpus = manifest["corpus"]
    command = [
        str(cli),
        "--translator", "nmt",
        "--nmt-model", str(ROOT / manifest["model"]),
        "--nmt-threads", str(manifest["intra_threads"]),
        "--beam", str(manifest["beam_size"]),
        "--target", target,
        "--corpus", f"user={ROOT / corpus['user']}",
        "--corpus", f"domain={ROOT / corpus['domain']}",
        "--corpus", f"general={ROOT / corpus['general']}",
        "--rules", str(ROOT / manifest["rules"]),
        "--text-file", str(text_file),
        "--json-lines",
    ]
    if manifest.get("protect", True):
        command += ["--protect-terms"]
    if scene:
        command += ["--scene", scene]
    if window:
        command += ["--window", window]
    return command


def run_group(cli: Path, manifest: dict, key: tuple[str, str, str],
              cases: list[dict]) -> list[dict] | None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n",
                                     suffix=".txt", delete=False) as handle:
        text_file = Path(handle.name)
        for case in cases:
            handle.write(case["source"] + "\n")
    try:
        process = subprocess.run(
            cli_command(cli, manifest, key, text_file),
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
    finally:
        text_file.unlink(missing_ok=True)

    if process.returncode != 0:
        print(f"FAIL group {key}: exit {process.returncode}", file=sys.stderr)
        tail = (process.stderr or "").strip().splitlines()[-6:]
        for line in tail:
            print(f"  {line}", file=sys.stderr)
        return None

    payloads = []
    for number, raw in enumerate(process.stdout.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            print(f"FAIL group {key}: line {number} is not JSON", file=sys.stderr)
            return None
        if event.get("type") != "subtitle":
            print(f"FAIL group {key}: line {number} is not a subtitle", file=sys.stderr)
            return None
        payloads.append(event.get("data") or {})
    if len(payloads) != len(cases):
        print(
            f"FAIL group {key}: expected {len(cases)} subtitles, got {len(payloads)}",
            file=sys.stderr,
        )
        return None
    return payloads


def normalise(value):
    return None if value in (None, "") else value


def compare(expected: dict, actual: dict) -> list[str]:
    differences = []
    for field in COMPARE_FIELDS:
        want = normalise(expected.get(field))
        got = normalise(actual.get(field))
        if want != got:
            differences.append(f"{field}: expected {want!r}, got {got!r}")
    return differences


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--manifest", type=Path, default=ROOT / "golden" / "nmt" / "manifest.json")
    parser.add_argument("--show", type=int, default=10, help="failures to print")
    args = parser.parse_args(argv)

    if not args.cli.exists():
        print(f"FAIL engine binary not found: {args.cli}", file=sys.stderr)
        return 2
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    cases = load_cases(args.cases)
    if len(cases) != manifest.get("cases"):
        print(
            f"FAIL manifest says {manifest.get('cases')} cases, file has {len(cases)}",
            file=sys.stderr,
        )
        return 2

    failures: list[str] = []
    compared = 0
    for key, group in group_cases(cases):
        payloads = run_group(args.cli, manifest, key, group)
        if payloads is None:
            return 1
        for case, actual in zip(group, payloads):
            compared += 1
            differences = compare(case["expected"], actual)
            if differences:
                failures.append(
                    f"{case['id']} [{case['target_lang']}|{case['scene']}|{case['window']}] "
                    + "; ".join(differences)
                )

    if failures:
        for line in failures[: args.show]:
            print(f"FAIL {line}")
        if len(failures) > args.show:
            print(f"... {len(failures) - args.show} more failure(s)")
        print(f"{compared - len(failures)}/{compared} cases match", file=sys.stderr)
        return 1

    print(f"{compared}/{compared} NMT cases match the Python reference")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
