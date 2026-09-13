"""Example plugin: export the subtitle history as SRT or as a glossary.

The ``export`` contract in one file, covering both shapes a caller might want:
a timed subtitle file, and a plain term list. Which one runs is chosen by the
``format`` attribute on the object, so one module can provide several formats.

``payload`` is whatever the caller hands over -- by convention::

    {
      "entries": [
        {"start": 12.5, "end": 15.0, "source": "...", "target": "..."},
        ...
      ]
    }

Only ``start``/``end`` are optional; without them SRT uses a nominal duration so
the file is still valid rather than missing entries.
"""

from __future__ import annotations

from typing import Any

PLUGIN = {
    "name": "export-formats",
    "version": "0.1",
    "api": 1,
    "description": "SRT subtitles and a bilingual glossary, from the session history",
    "points": ["export"],
}

#: Used when an entry has no timings, so the output stays a valid subtitle file.
NOMINAL_DURATION = 2.0


def _timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    millis = int(round((seconds - int(seconds)) * 1000)) % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


class SrtExporter:
    """A standard .srt file, one cue per entry."""

    format = "srt"

    def export(self, payload: Any, options: dict[str, Any] | None = None) -> str:
        entries = _entries(payload)
        options = options or {}
        bilingual = bool(options.get("bilingual", False))

        blocks: list[str] = []
        cursor = 0.0
        for index, entry in enumerate(entries, start=1):
            start = float(entry.get("start", cursor))
            end = float(entry.get("end", start + NOMINAL_DURATION))
            cursor = end

            if bilingual and entry.get("source"):
                body = f"{entry['source']}\n{entry.get('target', '')}".strip()
            else:
                body = str(entry.get("target") or entry.get("source") or "").strip()
            if not body:
                continue
            blocks.append(f"{index}\n{_timestamp(start)} --> {_timestamp(end)}\n{body}\n")
        return "\n".join(blocks)


class GlossaryExporter:
    """A bilingual term list, one pair per line."""

    format = "glossary"

    def export(self, payload: Any, options: dict[str, Any] | None = None) -> str:
        options = options or {}
        separator = str(options.get("separator", "\t"))
        header = options.get("header", True)

        seen: set[tuple[str, str]] = set()
        rows: list[str] = []
        if header:
            rows.append(separator.join(("source", "target")))
        for entry in _entries(payload):
            source = str(entry.get("source") or "").strip()
            target = str(entry.get("target") or "").strip()
            if not source or not target or source == target:
                continue
            key = (source, target)
            if key in seen:
                continue
            seen.add(key)
            rows.append(separator.join((source, target)))
        return "\n".join(rows) + ("\n" if rows else "")


def _entries(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, dict):
        entries = payload.get("entries")
        if isinstance(entries, list):
            return [e for e in entries if isinstance(e, dict)]
        return []
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    return []


def register() -> dict[str, object]:
    # One module can provide several implementations of the same point; the
    # registry keys them by their `format` attribute.
    return {"export": [SrtExporter(), GlossaryExporter()]}
