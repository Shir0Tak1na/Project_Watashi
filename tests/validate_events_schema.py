#!/usr/bin/env python3
"""Hold the CLI's real output to schema/events.schema.json, and prove the
schema is still doing work.

``tools/validate_stream.py`` checks the stream mechanics (ready first, seq
increasing, stopped last). It deliberately does not know the payload shapes, so
a ``ready`` that forgot ``region`` would sail through it. That is exactly the
false-green this script exists to catch:

1. every event the CLI wrote must validate against the generated schema;
2. deleting ``data.region`` from the ``ready`` event must make it fail.

``jsonschema`` is a CI-only dependency on purpose: the prototype's own checks
must keep running on a machine without it, and this file is only invoked from
the C++ workflow.

    python tests/validate_events_schema.py build/stream.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schema" / "events.schema.json"


def load_events(path: Path) -> list[Any]:
    events = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            raise SystemExit(f"{path}:{number}: blank line")
        events.append(json.loads(raw))
    return events


def errors_for(validator: Any, events: list[Any], label: str) -> list[str]:
    errors = []
    for index, event in enumerate(events, 1):
        for error in validator.iter_errors(event):
            errors.append(f"{label}:{index}: {error.message}")
    return errors


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stream", type=Path)
    args = parser.parse_args(argv)

    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        print("jsonschema is not installed; this check is CI-only", file=sys.stderr)
        return 2

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    events = load_events(args.stream)
    if not events:
        print(f"FAIL {args.stream}: no events")
        return 1

    errors = errors_for(validator, events, str(args.stream))
    if errors:
        for error in errors:
            print(f"FAIL {error}")
        return 1
    print(f"OK {args.stream}: {len(events)} event(s) match events.schema.json")

    # Negative control. A schema that accepts everything is not evidence, so the
    # required field test uses the same validator on a deliberately broken copy.
    broken = json.loads(json.dumps(events))
    ready = next(
        (event for event in broken if event.get("type") == "ready"), None
    )
    if ready is None:
        print("FAIL the stream has no ready event to break")
        return 1
    if "region" not in ready.get("data", {}):
        print("FAIL the ready event has no data.region to remove")
        return 1
    del ready["data"]["region"]

    negative = errors_for(validator, broken, "<ready without region>")
    if not negative:
        print("FAIL ready without data.region was accepted; the schema is not "
              "enforcing its required fields")
        return 1
    print(f"OK rejected: ready without data.region -> {negative[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
