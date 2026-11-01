"""Export the Python settings schema as config/settings_ui.json.

The C++ settings window renders this file; it hard-codes no field. Re-run this
after changing prototype/watashi/settings_schema.py and commit both the schema
change and the regenerated JSON.

    python tools/export_settings_ui.py            # write the file
    python tools/export_settings_ui.py --check    # fail if it is out of date

The mapping is deliberately total: every schema ``kind`` lands on one of the
UI's six widget types, while the original kind travels beside it so the model
still knows a path_list from a path and a region from a plain string.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from watashi import settings_schema as schema  # noqa: E402

sys.dont_write_bytecode = True

OUTPUT = ROOT / "config" / "settings_ui.json"

#: The only widget types the native settings window knows how to build.
TYPE_MAP = {
    "bool": "bool",
    "int": "int",
    "float": "float",
    "enum": "enum",
    "text": "string",
    "path": "path",
    "path_list": "path",
    "region": "string",
}


def field_dict(field: schema.Field) -> dict:
    widget = TYPE_MAP.get(field.kind)
    if widget is None:
        raise SystemExit(f"unmapped settings kind: {field.kind} ({field.key})")
    data = {
        "key": field.key,
        "label": field.label,
        "type": widget,
        "kind": field.kind,
        "default": field.default,
        "choices": list(field.choices),
        "scope": field.applies,
        "description": field.description,
    }
    if field.low is not None:
        data["low"] = field.low
    if field.high is not None:
        data["high"] = field.high
    for name in ("unit", "command", "note", "cost", "danger"):
        value = getattr(field, name)
        if value:
            data[name] = value
    return data


def build() -> dict:
    keys: set[str] = set()
    groups = []
    for category in schema.CATEGORIES:
        fields = []
        for field in category.fields:
            if field.key in keys:
                raise SystemExit(f"duplicate settings key: {field.key}")
            keys.add(field.key)
            fields.append(field_dict(field))
        groups.append(
            {
                "id": category.id,
                "title": category.title,
                "summary": category.summary,
                "fields": fields,
            }
        )
    return {
        "version": 1,
        "source": "prototype/watashi/settings_schema.py",
        "live_count": sum(1 for f in schema.all_fields() if f.applies == schema.LIVE),
        "restart_count": sum(1 for f in schema.all_fields() if f.applies == schema.RESTART),
        "groups": groups,
    }


def render(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="do not write; exit 1 when the committed file differs")
    parser.add_argument("--output", type=Path, default=OUTPUT,
                        help=f"destination (default: {OUTPUT.relative_to(ROOT)})")
    args = parser.parse_args(argv)

    text = render(build())
    output: Path = args.output
    if args.check:
        try:
            current = output.read_text(encoding="utf-8")
        except OSError as error:
            print(f"missing {output}: {error}", file=sys.stderr)
            return 1
        if current != text:
            print(f"{output} is out of date; run tools/export_settings_ui.py",
                  file=sys.stderr)
            return 1
        print(f"{output} is up to date")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {output} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
