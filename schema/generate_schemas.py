#!/usr/bin/env python3
"""Generate the three W0.3 JSON Schemas from the Python source of truth.

The schemas are *derived*: constants, dataclasses and the settings registry are
imported from ``prototype/watashi`` and turned into JSON Schema. Nothing in
``schema/*.json`` is written by hand; edit this generator and re-run instead.

    .venv/Scripts/python.exe schema/generate_schemas.py

Outputs:
    schema/events.schema.json
    schema/settings.schema.json
    schema/presentation.schema.json
"""

from __future__ import annotations

import dataclasses
import json
import sys
import types
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from watashi import __version__  # noqa: E402
from watashi import events as ev  # noqa: E402
from watashi import local_nmt  # noqa: E402
from watashi import presentation  # noqa: E402
from watashi import settings_schema  # noqa: E402

SCHEMA_DIR = ROOT / "schema"
BASE_ID = "https://project-watashi.invalid/schema"
DRAFT = "https://json-schema.org/draft/2020-12/schema"


# --------------------------------------------------------------------------- #
# type introspection
# --------------------------------------------------------------------------- #


def ref(name: str) -> dict:
    return {"$ref": f"#/$defs/{name}"}


def type_schema(annotation: object, defs: dict[str, dict], seen: set[str]) -> dict:
    """Translate one Python annotation into JSON Schema."""
    if annotation is type(None):
        return {"type": "null"}
    if annotation in (str,):
        return {"type": "string"}
    if annotation in (bool,):
        return {"type": "boolean"}
    if annotation in (int,):
        return {"type": "integer"}
    if annotation in (float,):
        return {"type": "number"}
    if annotation is typing.Any:
        return {}

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if origin in (typing.Union, types.UnionType):
        return {"anyOf": [type_schema(arg, defs, seen) for arg in args]}

    if origin is list:
        item = type_schema(args[0] if args else typing.Any, defs, seen)
        return {"type": "array", "items": item}

    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return {"type": "array", "items": type_schema(args[0], defs, seen)}
        return {
            "type": "array",
            "prefixItems": [type_schema(arg, defs, seen) for arg in args],
            "minItems": len(args),
            "maxItems": len(args),
        }

    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        name = annotation.__name__
        if name not in seen:
            seen.add(name)
            defs[name] = dataclass_schema(annotation, defs, seen)
        return ref(name)

    return {}


def dataclass_schema(
    cls: type, defs: dict[str, dict], seen: set[str], required: bool = True
) -> dict:
    hints = typing.get_type_hints(cls)
    properties: dict[str, dict] = {}
    mandatory: list[str] = []
    for field in dataclasses.fields(cls):
        annotation = hints.get(field.name, field.type)
        schema = type_schema(annotation, defs, seen)
        default = field.default
        if default is not dataclasses.MISSING:
            try:
                schema = {**schema, "default": default}
            except TypeError:
                pass
        properties[field.name] = schema
        if field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
            mandatory.append(field.name)
    result: dict = {"type": "object", "properties": properties}
    if required:
        result["required"] = sorted(properties)
    elif mandatory:
        result["required"] = mandatory
    result["additionalProperties"] = False
    return result


def encoded_pairs(func: object) -> list[tuple[str, str]]:
    """(wire key, source attribute) pairs read from an encoder's return literal."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            pairs: list[tuple[str, str]] = []
            for key, value in zip(node.value.keys, node.value.values):
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                    continue
                attributes = [
                    item.attr
                    for item in ast.walk(value)
                    if isinstance(item, ast.Attribute)
                ]
                if not attributes:
                    raise RuntimeError(
                        f"{func.__qualname__}: no attribute behind key {key.value!r}"
                    )
                pairs.append((key.value, attributes[0]))
            return pairs
    raise RuntimeError(f"could not find the returned dict in {func.__qualname__}")


def wire_dataclass_schema(cls: type, encoder: object) -> dict:
    """Dataclass -> schema under the names the encoder actually emits.

    The encoder is the wire contract: its return literal names the keys and names
    the dataclass attributes they came from. Fields the encoder does not emit
    (``timestamp``) are therefore absent from the schema, which is correct -- they
    never travel over the boundary.
    """
    pairs = encoded_pairs(encoder)
    field_names = {field.name for field in dataclasses.fields(cls)}
    hints = typing.get_type_hints(cls)
    defs: dict[str, dict] = {}
    seen: set[str] = set()
    properties: dict[str, dict] = {}
    for key, attribute in pairs:
        field_name = attribute if attribute in field_names else key
        if field_name not in field_names:
            raise RuntimeError(
                f"{cls.__name__}: encoder key {key!r} maps to unknown field {field_name!r}"
            )
        properties[key] = type_schema(hints[field_name], defs, seen)
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
        "additionalProperties": False,
        "$defs": defs,
    }


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #


def session_info_keys() -> list[str]:
    """The keys of ``Session.info``'s returned literal, read from source.

    ``ready`` is the first event a client reads, so its payload has to be pinned
    by the schema. The session is not imported here: the return literal in
    ``session.py`` is the source of truth, and parsing it keeps this generator
    independent of a configured model or a screen.
    """
    import ast

    tree = ast.parse((ROOT / "prototype" / "watashi" / "session.py").read_text("utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Session":
            continue
        for child in node.body:
            if not isinstance(child, ast.FunctionDef) or child.name != "info":
                continue
            for item in ast.walk(child):
                if isinstance(item, ast.Return) and isinstance(item.value, ast.Dict):
                    keys = [
                        key.value
                        for key in item.value.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    ]
                    if keys:
                        return keys
    raise RuntimeError("could not find Session.info's returned dict keys")


def build_ready_schema() -> dict:
    properties: dict[str, dict] = {key: {} for key in session_info_keys()}
    properties["schema_version"] = {
        "type": "integer",
        "const": ev.SCHEMA_VERSION,
    }
    properties["region_box"] = {
        "anyOf": [
            {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 4,
                "maxItems": 4,
            },
            {"type": "null"},
        ]
    }
    return {
        "type": "object",
        "properties": properties,
        # Only the fields a reader must understand are required. Pinning all
        # forty Session.info keys made every new surface a schema change and
        # made a skeleton that knows just its own region illegal, even though
        # the payload is open for additive evolution. The contract a client
        # actually keys on is: which version, and where the region is.
        "required": ["region", "region_box", "schema_version"],
        "additionalProperties": True,
    }


#: Payload objects whose property sets may grow without breaking a v1 reader.
#: ``StatusInfo`` is deliberately absent: it carries one free-form message and is
#: not an extension surface of its own.
OPEN_PAYLOADS = (
    "TranslatedLine",
    "OverlayUpdate",
    "OverlayStats",
    "Refinement",
    "ReadyInfo",
    "ErrorInfo",
)


def build_events_schema() -> dict:
    line_schema = wire_dataclass_schema(ev.TranslatedLine, ev.TranslatedLine.as_dict)
    update_schema = wire_dataclass_schema(ev.OverlayUpdate, ev.encode_update)
    stats_schema = wire_dataclass_schema(ev.OverlayStats, ev.encode_stats)
    refinement_schema = wire_dataclass_schema(local_nmt.Refinement, ev.encode_refinement)

    # The update schema nests lines; keep it as a reference rather than inline.
    update_schema["properties"]["lines"] = {
        "type": "array",
        "items": {"$ref": "#/$defs/TranslatedLine"},
    }
    defs: dict[str, dict] = {
        "TranslatedLine": line_schema,
        "OverlayUpdate": update_schema,
        "OverlayStats": stats_schema,
        "Refinement": refinement_schema,
        "ReadyInfo": build_ready_schema(),
        "StatusInfo": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        "ErrorInfo": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["command", "message"],
            "additionalProperties": False,
        },
    }
    # Pull nested $defs produced by wire_dataclass_schema (none currently) into
    # the root, where the $ref strings point.
    for name in list(defs):
        nested = defs[name].pop("$defs", {})
        for nested_name, nested_schema in nested.items():
            defs.setdefault(nested_name, nested_schema)

    # Payloads are the additive-evolution surface: a later minor revision may add
    # keys (segments, t_start, ...) and a v1 reader must keep accepting the event,
    # ignoring what it does not know. The envelope above stays closed because
    # v/type/ts/data/seq *is* the contract -- an unknown key there means a
    # different protocol, not a richer payload.
    for name in OPEN_PAYLOADS:
        defs[name]["additionalProperties"] = True

    event_schema = {
        "if": {"properties": {"type": {"const": "subtitle"}}, "required": ["type"]},
        "then": {
            "properties": {
                "data": {"$ref": "#/$defs/OverlayUpdate"},
            }
        },
    }
    ready_condition = {
        "if": {"properties": {"type": {"const": "ready"}}, "required": ["type"]},
        "then": {"properties": {"data": {"$ref": "#/$defs/ReadyInfo"}}},
    }
    stats_condition = {
        "if": {"properties": {"type": {"const": "stats"}}, "required": ["type"]},
        "then": {"properties": {"data": {"$ref": "#/$defs/OverlayStats"}}},
    }
    refinement_condition = {
        "if": {"properties": {"type": {"const": "refinement"}}, "required": ["type"]},
        "then": {"properties": {"data": {"$ref": "#/$defs/Refinement"}}},
    }
    status_condition = {
        "if": {"properties": {"type": {"const": "status"}}, "required": ["type"]},
        "then": {"properties": {"data": {"$ref": "#/$defs/StatusInfo"}}},
    }
    error_condition = {
        "if": {"properties": {"type": {"const": "error"}}, "required": ["type"]},
        "then": {"properties": {"data": {"$ref": "#/$defs/ErrorInfo"}}},
    }

    return {
        "$schema": DRAFT,
        "$id": f"{BASE_ID}/events-{ev.SCHEMA_VERSION}.json",
        "title": f"Project Watashi event envelope v{ev.SCHEMA_VERSION}",
        "x-schema-version": ev.SCHEMA_VERSION,
        "x-source": "prototype/watashi/events.py",
        "type": "object",
        "properties": {
            "v": {"const": ev.SCHEMA_VERSION},
            "type": {"enum": list(ev.ALL_EVENTS)},
            "ts": {"type": "number"},
            "seq": {"type": "integer", "minimum": 1},
            "data": {"type": "object"},
        },
        "required": ["v", "type", "ts", "data"],
        "additionalProperties": False,
        "allOf": [
            event_schema,
            ready_condition,
            stats_condition,
            refinement_condition,
            status_condition,
            error_condition,
        ],
        "$defs": defs,
    }


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #


def field_schema(field: settings_schema.Field) -> dict:
    if field.kind == "bool":
        schema: dict = {"type": "boolean"}
    elif field.kind == "int":
        schema = {"type": ["integer", "null"] if field.default is None else "integer"}
    elif field.kind == "float":
        schema = {"type": ["number", "null"] if field.default is None else "number"}
    elif field.kind == "enum":
        if field.default is None:
            schema = {
                "type": ["string", "null"],
                "enum": [*field.choices, None],
            }
        else:
            schema = {"type": "string", "enum": list(field.choices)}
    elif field.kind == "path_list":
        schema = {"type": "array", "items": {"type": "string"}}
    elif field.kind in ("text", "path", "region"):
        schema = {"type": ["string", "null"]}
    else:
        schema = {}
    if field.kind in ("int", "float"):
        if field.low is not None:
            schema["minimum"] = field.low
        if field.high is not None:
            schema["maximum"] = field.high
    if field.default is not None or field.kind in ("text", "path", "region"):
        schema["default"] = field.default
    schema["x-applies"] = field.applies
    if field.command:
        schema["x-command"] = field.command
    return schema


def build_settings_schema() -> dict:
    root_properties: dict[str, dict] = {}
    for category in settings_schema.CATEGORIES:
        section = category.id
        root_properties.setdefault(
            section,
            {
                "type": "object",
                "title": category.title,
                "description": category.summary,
                "properties": {},
                "additionalProperties": False,
            },
        )
        for field in category.fields:
            parts = field.key.split(".")
            target = root_properties[section]
            for part in parts[1:-1]:
                target = target["properties"].setdefault(
                    part,
                    {"type": "object", "properties": {}, "additionalProperties": False},
                )
            target["properties"][parts[-1]] = field_schema(field)
    return {
        "$schema": DRAFT,
        "$id": f"{BASE_ID}/settings-{__version__}.json",
        "title": f"Project Watashi settings {__version__}",
        "x-schema-version": __version__,
        "x-source": "prototype/watashi/settings_schema.py",
        "type": "object",
        "properties": root_properties,
        "additionalProperties": False,
    }


# --------------------------------------------------------------------------- #
# presentation
# --------------------------------------------------------------------------- #


def build_presentation_schema() -> dict:
    defs: dict[str, dict] = {}
    seen: set[str] = set()
    root = ref("PresentationSpec")
    type_schema(presentation.PresentationSpec, defs, seen)
    root_schema = defs["PresentationSpec"]
    root_schema["properties"]["version"] = {
        "const": presentation.SPEC_VERSION,
        "default": presentation.SPEC_VERSION,
    }
    return {
        "$schema": DRAFT,
        "$id": f"{BASE_ID}/presentation-{presentation.SPEC_VERSION}.json",
        "title": f"Project Watashi presentation spec v{presentation.SPEC_VERSION}",
        "x-schema-version": presentation.SPEC_VERSION,
        "x-source": "prototype/watashi/presentation.py",
        **root,
        "$defs": defs,
    }


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #


def write(name: str, schema: dict) -> None:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    path = SCHEMA_DIR / name
    path.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")


def main() -> int:
    write("events.schema.json", build_events_schema())
    write("settings.schema.json", build_settings_schema())
    write("presentation.schema.json", build_presentation_schema())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
