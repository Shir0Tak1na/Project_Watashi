#!/usr/bin/env python3
"""Contract check for W0.7's additive payload room and internal-only fields.

W0.7 adds two kinds of data without adding behaviour, and both are easy to
undo by accident:

1. **Payload schemas are open, the envelope is not.** A later revision may add
   ``segments`` or ``t_start`` to a subtitle and a v1 client must keep reading
   it. If the payload definition goes back to ``additionalProperties: false``,
   that evolution turns into a breaking change -- and nothing in the stream
   validator would notice, because it only checks the events the engine emits
   today.
2. **Timestamps and utterance ids stay internal.** ``TranslatedLine`` carries
   ``t_start``/``t_end``/``utterance_id``/``revision`` for the engine's own
   bookkeeping, but ``as_dict`` must not emit them. If one leaks, every
   ``--json-lines`` consumer and the golden corpus see a byte the contract does
   not describe.

The API in this module is ``decode_update``: an unknown key in a payload is
data the reader does not need, not an error. These checks feed it exactly that
shape.

    run.cmd selfcheck_contract
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watashi.checks import Checker  # noqa: E402

from watashi import events as ev  # noqa: E402
from watashi.events import TranslatedLine, decode_envelope, decode_update  # noqa: E402
from watashi.ocr import OcrLine  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schema"
GOLDEN_CASES = ROOT / "golden" / "cases.jsonl"

#: Hash of the frozen golden corpus, taken at the W0.7 baseline (HEAD 94a8f0b).
#: This check exists because "only add data" stops being true the moment the
#: corpus is regenerated: the 200 cases are the record of what the engine said
#: before, and rewriting them would hide any behaviour drift W0.7 introduced.
GOLDEN_CASES_SHA256 = "44bdecc09e03dbd60fb7c46c088131fc049e3c70a431183b946e12532a122597"

#: Payload definitions W0.7 opened up. StatusInfo is not one of them.
OPEN_PAYLOADS = (
    "TranslatedLine",
    "OverlayUpdate",
    "OverlayStats",
    "Refinement",
    "ReadyInfo",
    "ErrorInfo",
)

#: The four keys that must never be emitted by ``TranslatedLine.as_dict``.
INTERNAL_LINE_KEYS = ("t_start", "t_end", "utterance_id", "revision")

WIRE_LINE_KEYS = ("source", "target", "box", "confidence", "coverage")

WIRE_UPDATE_KEYS = (
    "source",
    "target",
    "coverage",
    "confidence",
    "latency_ms",
    "trace",
    "refined",
    "backend",
    "lines",
)


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def main() -> int:
    check = Checker()
    print("=" * 78)
    print("Contract self check (no screen, no models, no network)")
    print("=" * 78)

    # ---------------------------------------------------------------- #
    check.section("the three schemas parse")
    schemas: dict[str, dict] = {}
    for name in ("events.schema.json", "settings.schema.json", "presentation.schema.json"):
        try:
            schemas[name] = load_schema(name)
            check.check(f"{name} is valid JSON", isinstance(schemas[name], dict))
        except json.JSONDecodeError as exc:
            check.check(f"{name} is valid JSON", False, str(exc))

    events_schema = schemas.get("events.schema.json", {})
    defs = events_schema.get("$defs", {})

    # ---------------------------------------------------------------- #
    check.section("payloads are open, the envelope is closed")
    closed = [
        name
        for name in OPEN_PAYLOADS
        if defs.get(name, {}).get("additionalProperties") is not True
    ]
    check.check(
        "every payload definition accepts unknown keys",
        not closed,
        f"still closed: {closed}" if closed else f"{len(OPEN_PAYLOADS)} payloads open",
    )
    check.check(
        "the envelope itself stays closed",
        events_schema.get("additionalProperties") is False,
        f"additionalProperties={events_schema.get('additionalProperties')!r}",
    )
    envelope_properties = set(events_schema.get("properties", {}))
    check.check(
        "the envelope still describes exactly v/type/ts/seq/data",
        envelope_properties == {"v", "type", "ts", "seq", "data"},
        f"properties={sorted(envelope_properties)}",
    )
    check.check(
        "the schema still speaks v1",
        events_schema.get("x-schema-version") == 1
        and events_schema.get("properties", {}).get("v", {}).get("const") == 1,
    )

    # ---------------------------------------------------------------- #
    check.section("a v1 reader accepts a payload it does not fully know")
    payload = {
        "source": "the sword intent of this sect is a myth",
        "target": "本门剑意只是传说",
        "coverage": 1.0,
        "confidence": 0.98,
        "latency_ms": 12.5,
        "trace": "corpus",
        "refined": False,
        "backend": "corpus",
        "segments": [],  # the additive key W0.7 is making room for
        "lines": [
            {
                "source": "the sword intent of this sect is a myth",
                "target": "本门剑意只是传说",
                "box": [1, 2, 3, 4],
                "confidence": 0.98,
                "coverage": 1.0,
                "segments": [],
            }
        ],
    }
    try:
        update = decode_update(payload)
        accepted = True
        detail = f"{len(update.lines)} line(s), target={update.target_text!r}"
    except Exception as exc:  # noqa: BLE001 - the failure itself is the result
        update = None
        accepted = False
        detail = f"{type(exc).__name__}: {exc}"
    check.check("decode_update accepts a payload carrying segments", accepted, detail)

    if update is not None:
        check.check(
            "and ignores the unknown key instead of inventing a field",
            not hasattr(update, "segments")
            and not hasattr(update.lines[0], "segments"),
            "segments must not become part of the model",
        )
        check.check(
            "while still decoding every field it does know",
            update.target_text == payload["target"]
            and update.lines[0].target == payload["lines"][0]["target"],
        )

        envelope = ev.encode_event(ev.EVENT_SUBTITLE, payload, seq=7)
        try:
            event_type, data = decode_envelope(envelope)
            data_ok = event_type == ev.EVENT_SUBTITLE and data.get("segments") == []
        except Exception as exc:  # noqa: BLE001
            data_ok = False
            detail = f"{type(exc).__name__}: {exc}"
        check.check(
            "the whole subtitle envelope survives decode_envelope",
            data_ok,
        )

    # ---------------------------------------------------------------- #
    check.section("internal fields stay off the wire")
    line = TranslatedLine(
        source="src",
        target="dst",
        box=(10, 20, 30, 40),
        confidence=0.5,
        coverage=0.75,
        t_start=1234.5,
        t_end=1234.56,
        utterance_id=3,
        revision=0,
    )
    line_dict = line.as_dict()
    check.check(
        "as_dict emits exactly the five wire keys",
        tuple(line_dict) == WIRE_LINE_KEYS,
        f"keys={list(line_dict)}",
    )
    check.check(
        "and none of the internal keys leaks",
        not any(key in line_dict for key in INTERNAL_LINE_KEYS),
        f"internal keys present: {[k for k in INTERNAL_LINE_KEYS if k in line_dict]}",
    )

    update = ev.OverlayUpdate(
        source_text="src",
        target_text="dst",
        latency_ms=1.0,
        lines=[line],
    )
    encoded = ev.encode_update(update)
    check.check(
        "encode_update still emits exactly the nine envelope keys",
        tuple(encoded) == WIRE_UPDATE_KEYS,
        f"keys={list(encoded)}",
    )
    check.check(
        "encode_update is byte-identical to the pre-W0.7 payload",
        json.dumps(encoded, sort_keys=True)
        == json.dumps(
            {
                "source": "src",
                "target": "dst",
                "coverage": 1.0,
                "confidence": 1.0,
                "latency_ms": 1.0,
                "trace": "",
                "refined": False,
                "backend": "",
                "lines": [
                    {
                        "source": "src",
                        "target": "dst",
                        "box": [10, 20, 30, 40],
                        "confidence": 0.5,
                        "coverage": 0.75,
                    }
                ],
            },
            sort_keys=True,
        ),
    )

    # ---------------------------------------------------------------- #
    check.section("the new fields default to absent")
    check.check(
        "TranslatedLine defaults to no timestamps and utterance 0",
        TranslatedLine(source="s", target="t").t_start is None
        and TranslatedLine(source="s", target="t").t_end is None
        and TranslatedLine(source="s", target="t").utterance_id == 0
        and TranslatedLine(source="s", target="t").revision == 0,
    )
    check.check(
        "OcrLine has t_start/t_end and defaults them to None",
        OcrLine(text="t", confidence=1.0, box=(0, 0, 1, 1)).t_start is None
        and OcrLine(text="t", confidence=1.0, box=(0, 0, 1, 1)).t_end is None,
    )

    # ---------------------------------------------------------------- #
    check.section("the contract version is unchanged")
    check.check(
        "events.SCHEMA_VERSION is still 1",
        ev.SCHEMA_VERSION == 1,
        f"SCHEMA_VERSION={ev.SCHEMA_VERSION}",
    )

    # ---------------------------------------------------------------- #
    check.section("the golden corpus was not regenerated")
    digest = hashlib.sha256(GOLDEN_CASES.read_bytes()).hexdigest()
    check.check(
        "golden/cases.jsonl matches the pre-W0.7 hash",
        digest == GOLDEN_CASES_SHA256,
        f"sha256={digest}",
    )

    return check.report()


if __name__ == "__main__":
    raise SystemExit(main())
