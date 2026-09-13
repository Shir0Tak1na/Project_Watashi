"""The corpus the user edits in the UI, as opposed to by hand in a file (R2).

Two things this module is careful about, both of which it inherits from the same rule
that governs corrections:

* **It writes exactly one file, and never rewrites anyone else's.** Everything the
  editor produces goes into ``library.json`` in the user corpus layer -- the highest
  priority layer, which the ordinary corpus loader already reads. A user who maintains
  their own ``my_terms.json`` next to it keeps it byte for byte.
* **Editing the shipped library does not touch the shipped library.** The built-in
  corpora are demo data in the repository, tracked by git; rewriting them would dirty a
  working tree, conflict with the next pull, and destroy the difference between "what
  shipped" and "what I changed". So an edit to a built-in entry becomes a *user-layer
  entry with the same source*, which wins by layer precedence -- an override -- and the
  UI can show it as one and offer to revert it.

Suppression is the other half of editing the built-in library: overriding an entry
changes what it says, but there is no way to express "I do not want this shipped entry at
all" by writing an entry. ``_suppress`` is that statement, and the corpus loader honours
it for every layer below the user's.
"""

from __future__ import annotations

import csv
import io
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .correct import corrections_dir, write_json_atomic

#: Written into the user corpus layer, beside corrections.json
LIBRARY_FILE = "library.json"

_README = (
    "Written by the corpus editor: entries added or changed in the UI. Loaded as the "
    "user corpus layer, so these win over the shipped corpora. Editing a shipped entry "
    "writes an override here instead of rewriting the shipped file; _suppress hides a "
    "shipped entry altogether. Safe to edit or delete by hand."
)

#: Column names accepted on import, per field. Chinese and English, because a user
#: exporting from a spreadsheet will write whichever their tool produced.
_COLUMNS: dict[str, tuple[str, ...]] = {
    "source": ("source", "term", "原文", "源词", "原词", "词条"),
    "target": ("target", "translation", "译文", "翻译", "目标"),
    "lang": ("lang", "language", "target_lang", "语言", "目标语言"),
    "pos": ("pos", "part_of_speech", "词性"),
    "domain": ("domain", "领域", "领域标签"),
    "note": ("note", "comment", "备注", "说明"),
}

EXPORT_FORMATS = ("json", "csv", "tsv")


@dataclass
class LibraryEntry:
    """One entry the user wrote through the UI."""

    source: str
    target: str
    lang: str | None = None
    pos: str | None = None
    domain: str | None = None
    note: str | None = None
    updated: float = 0.0
    #: filled in by the session from what the loader actually loaded, not stored here
    overrides: str | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"target": self.target}
        if self.lang:
            data["lang"] = self.lang
        if self.pos:
            data["pos"] = self.pos
        if self.domain:
            data["domain"] = self.domain
        if self.note:
            data["note"] = self.note
        if self.updated:
            data["updated"] = round(self.updated, 3)
        return data

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "lang": self.lang or "",
            "pos": self.pos or "",
            "domain": self.domain or "",
            "note": self.note or "",
            "updated": self.updated,
            "layer": "user",
            "origin": f"corpus:{Path(LIBRARY_FILE).stem}",
            "user": True,
            "overrides": self.overrides,
        }


class Library:
    """The edited corpus file: read it, change it, write it whole.

    Keyed by ``(source, language)``, the same key the corpus loader uses. One row per
    source would have been simpler to draw in a table, and wrong: the engine supports the
    same term answered differently per target language, so a file that could not express
    it would make the editor weaker than the thing it edits -- and the user would find out
    by editing ``void`` for Japanese and watching their Chinese entry disappear.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.entries: dict[tuple[str, str], LibraryEntry] = {}
        self.suppressed: list[str] = []
        self.load()

    # -- loading ----------------------------------------------------------- #

    @staticmethod
    def key(source: str, lang: str | None) -> tuple[str, str]:
        return (source.strip(), (lang or "").strip())

    def load(self) -> None:
        entries: dict[tuple[str, str], LibraryEntry] = {}
        suppressed: list[str] = []
        data = self._read()
        if isinstance(data, dict):
            body = data.get("entries") if isinstance(data.get("entries"), dict) else {}
            for source, value in body.items():
                entry = self._parse(str(source), value)
                if entry is not None:
                    entries[self.key(entry.source, entry.lang)] = entry
            raw = data.get("_suppress")
            if isinstance(raw, list):
                suppressed = [str(item).strip() for item in raw if str(item).strip()]
        self.entries = entries
        self.suppressed = suppressed

    def _read(self) -> Any:
        if not self.path.exists():
            return None
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            # A hand edit with a stray comma loses this file for the run rather than
            # taking the engine down with it, and says so.
            print(f"[library] ignoring {self.path}: {exc}")
            return None

    @staticmethod
    def _parse(source: str, value: Any) -> LibraryEntry | None:
        if isinstance(value, str):
            return LibraryEntry(source=source, target=value)
        if not isinstance(value, dict):
            return None
        target = value.get("target") or value.get("translation") or value.get("text")
        if not isinstance(target, str) or not target.strip():
            return None
        raw_lang = value.get("lang") or value.get("target_lang") or value.get("language")
        return LibraryEntry(
            source=source,
            target=target,
            lang=str(raw_lang) if raw_lang else None,
            pos=value.get("pos"),
            domain=value.get("domain"),
            note=value.get("note"),
            updated=float(value.get("updated", 0.0) or 0.0),
        )

    # -- editing ----------------------------------------------------------- #

    def put(
        self,
        source: str,
        target: str,
        *,
        lang: str | None = None,
        pos: str | None = None,
        domain: str | None = None,
        note: str | None = None,
        now: float | None = None,
    ) -> tuple[LibraryEntry, bool]:
        """Add or change one entry. Returns ``(entry, created)``.

        Identified by source *and* language, so editing the Japanese answer for a term
        does not overwrite the Chinese one. Editing the same pair twice changes it rather
        than adding a rival row, which is what the table shows.
        """
        source = source.strip()
        target = target.strip()
        if not source:
            raise ValueError("an entry needs the source text")
        if not target:
            raise ValueError("an entry needs a translation")
        now = time.time() if now is None else now
        key = self.key(source, lang)
        existing = self.entries.get(key)
        created = existing is None
        entry = existing or LibraryEntry(source=source, target=target, lang=lang or None)
        entry.target = target
        entry.lang = lang or None
        entry.pos = pos or None
        entry.domain = domain or None
        entry.note = note or None
        entry.updated = now
        self.entries[key] = entry
        if source in self.suppressed:
            # Writing an entry for a source the user had hidden is an unambiguous
            # statement that they want it back.
            self.suppressed.remove(source)
        self.save()
        self.load()
        return self.entries.get(self.key(source, lang), entry), created

    def find(self, source: str, lang: str | None = None) -> LibraryEntry | None:
        """The entry for this source, optionally narrowed to one language.

        With no language and exactly one row for the source, that row is meant; with
        several, None is returned rather than an arbitrary choice, because "delete this
        term" against a term that exists in two languages has two different meanings and
        picking one silently is how the wrong row disappears.
        """
        source = source.strip()
        if lang is not None:
            return self.entries.get(self.key(source, lang))
        rows = self.for_source(source)
        return rows[0] if len(rows) == 1 else None

    def for_source(self, source: str) -> list[LibraryEntry]:
        source = source.strip()
        return [
            entry for (row_source, _lang), entry in self.entries.items()
            if row_source == source
        ]

    def delete(self, source: str, lang: str | None = None) -> bool:
        """Remove one entry. For an override this *is* the revert: the shipped entry
        underneath becomes visible again."""
        source = source.strip()
        key = self.key(source, lang) if lang is not None else None
        if key is None:
            rows = self.for_source(source)
            if len(rows) != 1:
                # Zero means nothing to delete; more than one means the caller has to say
                # which language, and both cases are reported rather than guessed at.
                return False
            key = self.key(rows[0].source, rows[0].lang)
        if self.entries.pop(key, None) is None:
            return False
        self.save()
        self.load()
        return True

    def suppress(self, source: str) -> bool:
        """Hide a shipped entry without pretending to translate it.

        Suppression is by *source*, not by (source, language): "I do not want this term"
        is a statement about the term, and hiding only the Chinese answer of a word would
        leave the same word arriving from the shipped corpus under another language.
        """
        source = source.strip()
        if not source or source in self.suppressed:
            return False
        # Overrides for a hidden term would be a contradiction: the term is hidden, so
        # they could never be reached.
        for entry in self.for_source(source):
            self.entries.pop(self.key(entry.source, entry.lang), None)
        self.suppressed.append(source)
        self.save()
        self.load()
        return True

    def unsuppress(self, source: str) -> bool:
        source = source.strip()
        if source not in self.suppressed:
            return False
        self.suppressed.remove(source)
        self.save()
        self.load()
        return True

    def merge(self, incoming: Iterable[LibraryEntry], *, replace: bool = True) -> dict[str, Any]:
        """Apply imported entries. Reports what each one did.

        ``replace`` is the only sensible default for an import that is meant to fix
        vocabulary: a file that says what a term should be overrides what was there. The
        alternative (skip existing) is kept for callers that want a purely additive
        import, and both are reported per entry so the user can see which happened.
        """
        added, updated, skipped = [], [], []
        now = time.time()
        incoming = list(incoming)
        for entry in incoming:
            source = entry.source.strip()
            if not source or not entry.target.strip():
                skipped.append(source or "(empty)")
                continue
            key = self.key(source, entry.lang)
            existing = self.entries.get(key)
            if existing is not None and not replace:
                skipped.append(source)
                continue
            entry.source = source
            entry.updated = entry.updated or now
            self.entries[key] = entry
            if source in self.suppressed:
                self.suppressed.remove(source)
            (updated if existing is not None else added).append(source)
        self.save()
        self.load()
        return {
            "added": len(added),
            "updated": len(updated),
            "skipped": len(skipped),
            "added_sources": added[:50],
            "updated_sources": updated[:50],
            "skipped_sources": skipped[:50],
            "total": len(self.entries),
        }

    def save(self) -> None:
        """Write the whole file, grouping the languages of one source together.

        A source answered in one language is written as a plain object, which is what a
        hand-written corpus looks like. A source answered in two is written as a *list* of
        objects, because a JSON object cannot have two identical keys -- and the failure
        mode of trying would be silent: the file would look right and hold one of them.
        """
        payload: dict[str, Any] = {"_readme": _README}
        if self.suppressed:
            payload["_suppress"] = sorted(self.suppressed, key=str.lower)

        grouped: dict[str, list[LibraryEntry]] = {}
        for entry in self.entries.values():
            grouped.setdefault(entry.source, []).append(entry)

        body: dict[str, Any] = {}
        for source in sorted(grouped, key=str.lower):
            rows = sorted(grouped[source], key=lambda item: item.lang or "")
            body[source] = rows[0].to_json() if len(rows) == 1 else [r.to_json() for r in rows]
        payload["entries"] = body
        write_json_atomic(self.path, payload)

    # -- introspection ----------------------------------------------------- #

    def describe(self) -> list[dict[str, Any]]:
        return [
            entry.to_dict()
            for entry in sorted(
                self.entries.values(), key=lambda e: (e.source.lower(), e.lang or "")
            )
        ]

    def stats(self) -> dict[str, Any]:
        return {
            "library_entries": len(self.entries),
            "library_suppressed": len(self.suppressed),
            "library_file": str(self.path),
        }

    def __len__(self) -> int:
        return len(self.entries)


# --------------------------------------------------------------------------- #
# file handling: what the editor imports and exports
# --------------------------------------------------------------------------- #


def library_path(config: Any) -> Path:
    """Where the edited corpus lives: the user layer, beside corrections.json."""
    return corrections_dir(config) / LIBRARY_FILE


def _language_base(tag: str | None) -> str:
    """Reuse the engine's own language folding, so the view cannot disagree with it."""
    from .translate import _language_base as engine_language_base

    return engine_language_base(tag)


def _same_language(left: str | None, right: str | None) -> bool:
    """Whether two language tags are the same language, for picking a row's shadow.

    An override written without a language is meant for whatever it replaced -- the user
    corrected a translation, they did not declare the term language neutral -- so it is
    treated as the same language as any sibling rather than as a stranger to all of them.
    """
    if not left or not right:
        return True
    return _language_base(left) == _language_base(right)


def corpus_view(corpus: Any, library: "Library") -> dict[str, Any]:
    """Every entry a corpus editor needs to show, from every layer, plus what is hidden.

    One list rather than one per layer, because that is the question the user is asking:
    "what will this term translate to, and where does that answer come from?" A row's
    ``user`` flag says the application wrote it, ``overrides`` names the shipped entry it
    is shadowing, and ``suppressed`` marks a shipped entry the user turned off -- the
    three states an editable corpus has, and each of them needs a different action
    (edit, revert, restore).
    """
    from .translate import normalize

    rows: list[dict[str, Any]] = []
    by_source: dict[str, list[Any]] = {}
    for entry in corpus.entries_snapshot() if corpus is not None else []:
        by_source.setdefault(normalize(entry.source), []).append(entry)

    # Keyed the way the edited file is: a source can be answered once per language, and
    # the note the user typed belongs to one of those answers, not to the source.
    mine: dict[tuple[str, str], LibraryEntry] = {}
    for (source, lang), entry in library.entries.items():
        mine[(normalize(source), _language_base(lang))] = entry

    def row(entry: Any, *, hidden: bool = False) -> dict[str, Any]:
        key = normalize(entry.source)
        siblings = by_source.get(key, [])
        shadowed = [
            other for other in siblings
            if (other.layer, other.origin, other.lang) != (entry.layer, entry.origin, entry.lang)
            and _same_language(other.lang, entry.lang)
        ]
        own = mine.get((key, _language_base(entry.lang)))
        return {
            "source": entry.source,
            "target": entry.target,
            "lang": entry.lang or "",
            "layer": entry.layer,
            "origin": entry.origin,
            "user": entry.layer == "user",
            "suppressed": hidden,
            #: the shipped entry this one is hiding or replacing, if any
            "overrides": shadowed[0].origin if shadowed else None,
            "overrides_target": shadowed[0].target if shadowed else None,
            "note": (own.note if own else None) or "",
            "updated": own.updated if own else 0.0,
        }

    for entry in corpus.entries_snapshot() if corpus is not None else []:
        rows.append(row(entry))
    for entry in corpus.hidden_entries() if corpus is not None else []:
        rows.append(row(entry, hidden=True))

    rows.sort(key=lambda item: (not item["user"], item["source"].lower()))
    return {
        "entries": rows,
        "active": sum(1 for item in rows if not item["suppressed"]),
        "user": sum(1 for item in rows if item["user"]),
        "suppressed": sum(1 for item in rows if item["suppressed"]),
        "overriding": sum(1 for item in rows if item["overrides"] and item["user"]),
        "languages": corpus.language_summary() if corpus is not None else "",
        "file": str(library.path),
        "export_formats": list(EXPORT_FORMATS),
    }


def effective_entries(corpus: Any, library: "Library", scope: str) -> list[dict[str, Any]]:
    """The rows to export: just the user's own, or everything the engine will use."""
    view = corpus_view(corpus, library)
    rows = view["entries"]
    if scope == "user":
        rows = [item for item in rows if item["user"] and not item["suppressed"]]
    else:
        rows = [item for item in rows if not item["suppressed"]]
    return rows


def _field_for(header: str) -> str | None:
    key = header.strip().lower().lstrip("\ufeff")
    for field_name, names in _COLUMNS.items():
        if key in names:
            return field_name
    return None


def parse_table(text: str, delimiter: str) -> tuple[list[LibraryEntry], list[str]]:
    """Read a CSV/TSV export, or a plain two-column list.

    Header driven when there is one, so a spreadsheet round trip loses nothing; a file
    with no recognisable header is read as ``source,target``, which is what a user
    pasting a glossary out of a document will have.
    """
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    problems: list[str] = []
    if not rows:
        return [], ["the file is empty"]

    first = [_field_for(cell) for cell in rows[0]]
    has_header = "source" in first and "target" in first
    mapping = first if has_header else None
    body = rows[1:] if has_header else rows

    entries: list[LibraryEntry] = []
    for index, row in enumerate(body, start=1 if has_header else 0):
        if mapping is None:
            if len(row) < 2:
                problems.append(f"line {index}: needs at least two columns")
                continue
            values = {"source": row[0], "target": row[1]}
        else:
            values = {}
            for position, field_name in enumerate(mapping):
                if field_name and position < len(row):
                    values[field_name] = row[position]
        source = (values.get("source") or "").strip()
        target = (values.get("target") or "").strip()
        if not source or not target:
            problems.append(f"line {index}: missing source or translation")
            continue
        entries.append(LibraryEntry(
            source=source,
            target=target,
            lang=(values.get("lang") or "").strip() or None,
            pos=(values.get("pos") or "").strip() or None,
            domain=(values.get("domain") or "").strip() or None,
            note=(values.get("note") or "").strip() or None,
        ))
    return entries, problems


def entries_from_payload(data: Any) -> tuple[list[LibraryEntry], list[str]]:
    """Turn the JSON corpus forms into entries.

    The same shapes the corpus loader accepts -- a bare mapping, ``{"entries": ...}``, or
    an object per entry -- so a file that works as a corpus imports without conversion.
    """
    problems: list[str] = []
    if not isinstance(data, dict):
        return [], ["the JSON root is not an object"]
    file_lang = data.get("lang") or data.get("target_lang") or data.get("language")
    body = data.get("entries") if isinstance(data.get("entries"), dict) else data
    entries: list[LibraryEntry] = []
    for source, value in body.items():
        if not isinstance(source, str) or not source.strip() or source.startswith("_"):
            continue
        if isinstance(value, str):
            entries.append(LibraryEntry(
                source=source, target=value, lang=str(file_lang) if file_lang else None
            ))
            continue
        if not isinstance(value, dict):
            problems.append(f"{source!r}: not a string or an object")
            continue
        target = value.get("target") or value.get("translation") or value.get("text")
        if not isinstance(target, str) or not target.strip():
            problems.append(f"{source!r}: no translation")
            continue
        raw_lang = value.get("lang") or value.get("target_lang") or value.get("language")
        entries.append(LibraryEntry(
            source=source,
            target=target,
            lang=str(raw_lang) if raw_lang else (str(file_lang) if file_lang else None),
            pos=value.get("pos"),
            domain=value.get("domain"),
            note=value.get("note"),
        ))
    if not entries and not problems:
        problems.append("no entries found in the file")
    # Sorted, so an import's report lists what it did in the same order every run: a
    # summary that reshuffles between runs is one nobody trusts.
    entries.sort(key=lambda item: (item.source.lower(), item.lang or ""))
    return entries, problems


def parse_import(text: str, fmt: str) -> tuple[list[LibraryEntry], list[str]]:
    """Parse imported text into entries, or explain why it could not be."""
    if not text.strip():
        return [], ["the file is empty"]
    if fmt in ("csv", "tsv"):
        return parse_table(text, "," if fmt == "csv" else "\t")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], [f"not valid JSON: {exc}"]
    return entries_from_payload(data)


def to_export(entries: list[dict[str, Any]], fmt: str) -> str:
    """Render entries for download.

    The ``effective`` export is meant to be usable as a corpus file, so JSON keeps the
    loader's own shape (a bare mapping plus a ``lang`` key per entry) rather than the
    editor's richer one.
    """
    if fmt in ("csv", "tsv"):
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter="," if fmt == "csv" else "\t", lineterminator="\n")
        writer.writerow(["source", "target", "lang", "pos", "domain", "note"])
        for entry in entries:
            writer.writerow([
                entry.get("source", ""),
                entry.get("target", ""),
                entry.get("lang", ""),
                entry.get("pos", ""),
                entry.get("domain", ""),
                entry.get("note", ""),
            ])
        return buffer.getvalue()

    payload: dict[str, Any] = {"_comment": "Exported by Project Watashi."}
    payload["entries"] = {
        entry["source"]: {
            key: entry[key]
            for key in ("target", "lang", "pos", "domain")
            if entry.get(key)
        }
        for entry in entries
        if entry.get("source") and entry.get("target")
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
