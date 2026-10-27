#!/usr/bin/env python3
"""Validate a Project Watashi NDJSON event stream using only the stdlib.

    python tools/validate_stream.py --selftest
    python tools/validate_stream.py golden/streams/screen.jsonl

Exit 0 when every line is valid, 1 otherwise. Errors name the line number and
the reason. No third-party package is imported: this must run in the C++ CI
image, where jsonschema is not installed.

The contract version and event-type whitelist are read from
``prototype/watashi/events.py`` with ``ast`` rather than imported, so the tool
stays usable without the Python engine's dependencies.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVENTS_SOURCE = ROOT / "prototype" / "watashi" / "events.py"


# --------------------------------------------------------------------------- #
# the 6 rejection cases come first; the validator is written to satisfy them
# --------------------------------------------------------------------------- #


def _valid_stream() -> list[dict[str, Any]]:
    return [
        {"v": 1, "type": "ready", "ts": 1.0, "seq": 1, "data": {"schema_version": 1}},
        {
            "v": 1,
            "type": "subtitle",
            "ts": 2.0,
            "seq": 2,
            "data": {
                "source": "the sword intent",
                "target": "剑意",
                "lines": [{"box": [1, 2, 3, 4]}],
            },
        },
        {
            "v": 1,
            "type": "stats",
            "ts": 3.0,
            "seq": 3,
            "data": {
                "fps": 1.0,
                "ocr_ms": 10.0,
                "translate_ms": 2.0,
                "total_ms": 12.0,
                "frames": 1,
                "skipped": 0,
                "corpus_entries": 84,
                "rules": 8,
                "cache_hit_rate": 0.0,
                "refinements": 0,
                "refinements_pending": 0,
                "refinements_dropped": 0,
                "memory_mib": 100.0,
            },
        },
        {"v": 1, "type": "stopped", "ts": 4.0, "seq": 4, "data": {}},
    ]


def _selftest_cases() -> list[tuple[str, list[dict[str, Any]]]]:
    cases: list[tuple[str, list[dict[str, Any]]]] = []

    missing_v = _valid_stream()
    del missing_v[0]["v"]
    cases.append(("missing v", missing_v))

    unknown_type = _valid_stream()
    unknown_type[1]["type"] = "caption"
    cases.append(("unknown type", unknown_type))

    duplicate_seq = _valid_stream()
    duplicate_seq[2]["seq"] = duplicate_seq[1]["seq"]
    cases.append(("duplicate seq", duplicate_seq))

    before_ready = _valid_stream()
    before_ready.insert(0, before_ready.pop(1))
    cases.append(("subtitle before ready", before_ready))

    stopped_not_last = _valid_stream()
    stopped_not_last.append(
        {"v": 1, "type": "status", "ts": 5.0, "seq": 5, "data": {"message": "late"}}
    )
    cases.append(("stopped not last", stopped_not_last))

    bad_box = _valid_stream()
    bad_box[1]["data"]["lines"][0]["box"] = [1, 2, 3]
    cases.append(("box length is not 4", bad_box))
    return cases


# --------------------------------------------------------------------------- #
# implementation
# --------------------------------------------------------------------------- #


def event_types() -> tuple[str, ...]:
    """Read EVENT_* constants and ALL_EVENTS from events.py without importing it."""
    try:
        tree = ast.parse(EVENTS_SOURCE.read_text(encoding="utf-8"))
    except OSError:
        return (
            "ready", "subtitle", "refinement", "stats", "status", "error",
            "settings", "correction", "library", "command_result", "stopped",
            "presentation",
        )
    constants: dict[str, str] = {}
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str) and target.id.startswith("EVENT_"):
                    constants[target.id] = node.value.value
            if (
                isinstance(target, ast.Name)
                and target.id == "ALL_EVENTS"
                and isinstance(node.value, (ast.Tuple, ast.List))
            ):
                names = [
                    item.id
                    for item in node.value.elts
                    if isinstance(item, ast.Name)
                ]
    resolved = tuple(constants[name] for name in names if name in constants)
    return resolved or tuple(constants.values())


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_box(value: Any) -> bool:
    return value is None or (
        isinstance(value, list)
        and len(value) == 4
        and all(_is_number(item) for item in value)
    )


#: Numeric stats fields and whether an integer is required. A bool is never a
#: number here: ``True`` silently passing as 1 is exactly the kind of wrong type
#: a stream validator exists to catch.
STATS_FIELDS: dict[str, str] = {
    "fps": "number",
    "ocr_ms": "number",
    "translate_ms": "number",
    "total_ms": "number",
    "frames": "integer",
    "skipped": "integer",
    "corpus_entries": "integer",
    "rules": "integer",
    "cache_hit_rate": "number",
    "refinements": "integer",
    "refinements_pending": "integer",
    "refinements_dropped": "integer",
    "memory_mib": "number",
}


def validate_stream(
    records: list[Any],
    source: str = "<memory>",
) -> list[str]:
    """Return one error string per problem; empty means valid."""
    allowed = set(event_types())
    errors: list[str] = []
    first_ready = False
    seen_ready = False
    previous_seq: int | None = None
    seen_stopped = False

    for line, record in enumerate(records, 1):
        where = f"{source}:{line}"
        if not isinstance(record, dict):
            errors.append(f"{where}: not a JSON object")
            continue

        version = record.get("v")
        if version != 1:
            errors.append(f"{where}: v must be 1, got {version!r}")
        event_type = record.get("type")
        if event_type not in allowed:
            errors.append(f"{where}: unknown event type {event_type!r}")
        if not _is_number(record.get("ts")):
            errors.append(f"{where}: ts must be a number")
        data = record.get("data")
        if not isinstance(data, dict):
            errors.append(f"{where}: data must be an object")
            data = {}

        seq = record.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            errors.append(f"{where}: seq must be an integer")
        else:
            if previous_seq is not None and seq <= previous_seq:
                errors.append(
                    f"{where}: seq must strictly increase; {seq} after {previous_seq}"
                )
            previous_seq = seq

        if line == 1 and event_type != "ready":
            errors.append(f"{where}: the first event must be ready, got {event_type!r}")
        if event_type == "ready":
            seen_ready = True
            first_ready = True
        if event_type == "subtitle" and not seen_ready:
            errors.append(f"{where}: subtitle before ready")

        if event_type == "stopped":
            if seen_stopped:
                errors.append(f"{where}: stopped appears more than once")
            seen_stopped = True
            if line != len(records):
                errors.append(f"{where}: stopped must be the last event")

        if event_type == "subtitle":
            lines = data.get("lines")
            if not isinstance(lines, list):
                errors.append(f"{where}: subtitle.data.lines must be an array")
            else:
                for index, item in enumerate(lines):
                    if not isinstance(item, dict):
                        errors.append(
                            f"{where}: lines[{index}] must be an object"
                        )
                        continue
                    if not _is_box(item.get("box")):
                        errors.append(
                            f"{where}: lines[{index}].box must be 4 numbers or null"
                        )

        if event_type == "stats":
            for field, kind in STATS_FIELDS.items():
                if field not in data:
                    continue
                value = data[field]
                ok = (
                    _is_number(value)
                    and (kind != "integer" or isinstance(value, int))
                )
                if not ok:
                    errors.append(
                        f"{where}: stats.{field} must be {kind}, got {type(value).__name__}"
                    )

    if records and not first_ready:
        errors.append(f"{source}:1: stream does not start with ready")
    return errors


def load_stream(path: Path) -> tuple[list[Any], list[str]]:
    records: list[Any] = []
    errors: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [], [f"{path}: cannot read: {exc}"]
    for line_number, raw in enumerate(lines, 1):
        if not raw.strip():
            errors.append(f"{path}:{line_number}: blank line")
            continue
        try:
            records.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            errors.append(f"{path}:{line_number}: invalid JSON: {exc.msg}")
    return records, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stream", nargs="?", type=Path, help="NDJSON file to validate")
    parser.add_argument("--selftest", action="store_true", help="run the 6 rejection cases")
    args = parser.parse_args(argv)

    if args.selftest:
        failed = 0
        for name, records in _selftest_cases():
            errors = validate_stream(records, "<selftest>")
            if errors:
                print(f"PASS rejected: {name} -> {errors[0]}")
            else:
                print(f"FAIL accepted: {name}")
                failed += 1
        return 0 if failed == 0 else 1

    if args.stream is None:
        parser.error("a stream file is required unless --selftest is used")

    records, errors = load_stream(args.stream)
    errors.extend(validate_stream(records, str(args.stream)))
    if errors:
        for error in errors:
            print(f"FAIL {error}")
        return 1
    print(f"OK {args.stream}: {len(records)} event(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
