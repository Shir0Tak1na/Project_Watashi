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
    "domain": ("domain", "scene", "领域", "场景"),
    "note": ("note", "comment", "备注", "说明"),
    # the conditions that separate two meanings of one term; see translate.Conditions
    "when_line": ("when_line", "条件行", "行条件"),
    "when_near": ("when_near", "when_nearby", "邻近词", "附近词"),
    "when_window": ("when_window", "窗口条件", "窗口"),
}

EXPORT_FORMATS = ("json", "csv", "tsv")


def domain_key(value: str | None) -> str:
    """The comparison key for a scene name. Mirrors ``translate._domain_key``.

    Duplicated on purpose and asserted equal in ``selfcheck_senses``: the editor and the
    engine disagreeing about whether ``Finance`` and ``finance`` are one scene would mean
    a user writing the second one and watching their first entry stop being used.
    """
    return (value or "").strip().casefold()


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
    #: Conditions that pick this meaning over another for the same term. Carried through
    #: the editor rather than only through hand-edited files: a table that silently
    #: dropped them would turn "edit this row's translation" into "remove the reason this
    #: row was the right one".
    when_line: str | None = None
    when_near: tuple[str, ...] = ()
    when_window: str | None = None
    #: filled in by the session from what the loader actually loaded, not stored here
    overrides: str | None = None

    @property
    def conditions(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.when_line:
            data["when_line"] = self.when_line
        if self.when_near:
            data["when_near"] = list(self.when_near)
        if self.when_window:
            data["when_window"] = self.when_window
        return data

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
        data.update(self.conditions)
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
            "when_line": self.when_line or "",
            "when_near": ", ".join(self.when_near),
            "when_window": self.when_window or "",
            "layer": "user",
            "origin": f"corpus:{Path(LIBRARY_FILE).stem}",
            "user": True,
            "overrides": self.overrides,
        }


def _as_near(value: object) -> tuple[str, ...]:
    """Normalise ``when_near`` from a form field, a list, or a comma-separated string."""
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),)


def _conditions_key(
    when_line: object = None, when_near: object = (), when_window: object = None
) -> tuple[str, tuple[str, ...], str]:
    """The identity of an entry's conditions, matching ``translate.Conditions.key()``.

    Conditions are part of *what makes two entries rivals*, so they have to be part of the
    editor's row identity too. Leaving them out -- which this did until a probe found it --
    meant a term with two meanings in one scene told apart only by ``when_line`` was one
    row in the editor: writing the second replaced the first, and a hand-written file
    holding both had one of them dropped the next time anything was saved from the panel.
    The engine supports the pair; the editor has to be able to express it.
    """
    return (
        str(when_line or "").strip(),
        _as_near(when_near),
        str(when_window or "").strip(),
    )


class Library:
    """The edited corpus file: read it, change it, write it whole.

    Keyed by ``(source, language, scene)``, the same three dimensions the corpus loader
    keys on. Keying on the source alone would have been simpler to draw in a table, and
    wrong: the engine answers one term differently per target language *and* per scene, so
    a file that could not express that would make the editor weaker than the thing it
    edits -- and the user would find out by writing the second meaning and watching the
    first one disappear. That is not hypothetical: it already happened twice, once for
    languages and once for scenes, and the second time was found by writing this check.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.entries: dict[tuple[str, str, str], LibraryEntry] = {}
        self.suppressed: list[str] = []
        self.load()

    # -- loading ----------------------------------------------------------- #

    @staticmethod
    def key(
        source: str,
        lang: str | None,
        domain: str | None = None,
        when_line: object = None,
        when_near: object = (),
        when_window: object = None,
    ) -> tuple[str, str, str, tuple[str, tuple[str, ...], str]]:
        """The row identity: term, language, scene **and conditions**.

        All four, because that is what the engine keys on. A row identity narrower than
        the engine's is not a simplification, it is a silent merge: two rows the engine
        keeps apart become one row here, and one of them is lost on the next save.
        """
        return (
            source.strip(),
            (lang or "").strip(),
            (domain or "").strip(),
            _conditions_key(when_line, when_near, when_window),
        )

    def load(self) -> None:
        entries: dict[tuple[Any, ...], LibraryEntry] = {}
        suppressed: list[str] = []
        data = self._read()
        if isinstance(data, dict):
            body = data.get("entries") if isinstance(data.get("entries"), dict) else {}
            for source, value in body.items():
                # A *list* means this source is answered more than once -- two languages,
                # one language in two scenes, or one scene with two conditions. Reading
                # only objects here dropped every such row on the floor, in the editor's
                # own file, including the shape this class writes: ``put`` saves and
                # reloads, so the second row for a term vanished from the table the moment
                # it was written, while the file on disk still held both -- and the next
                # save overwrote it with nothing.
                values = value if isinstance(value, list) else [value]
                for raw in values:
                    entry = self._parse(str(source), raw)
                    if entry is not None:
                        entries[self.key(
                            entry.source,
                            entry.lang,
                            entry.domain,
                            entry.when_line,
                            entry.when_near,
                            entry.when_window,
                        )] = entry
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
        raw_near = value.get("when_near") or value.get("when_nearby")
        near: tuple[str, ...] = ()
        if isinstance(raw_near, str):
            near = tuple(item.strip() for item in raw_near.split(",") if item.strip())
        elif isinstance(raw_near, (list, tuple)):
            near = tuple(str(item).strip() for item in raw_near if str(item).strip())
        return LibraryEntry(
            source=source,
            target=target,
            lang=str(raw_lang) if raw_lang else None,
            pos=value.get("pos"),
            domain=value.get("domain") or value.get("scene"),
            note=value.get("note"),
            updated=float(value.get("updated", 0.0) or 0.0),
            when_line=str(value["when_line"]).strip() if value.get("when_line") else None,
            when_near=near,
            when_window=str(value["when_window"]).strip() if value.get("when_window") else None,
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
        when_line: str | None = None,
        when_near: object = (),
        when_window: str | None = None,
    ) -> tuple[LibraryEntry, bool]:
        """Add or change one entry. Returns ``(entry, created)``.

        Identified by source *and* language *and* scene, so editing the Japanese answer for
        a term does not overwrite the Chinese one, and editing the financial sense does not
        overwrite the geographical one. Editing the same triple twice changes it rather
        than adding a rival row, which is what the table shows.
        """
        source = source.strip()
        target = target.strip()
        if not source:
            raise ValueError("an entry needs the source text")
        if not target:
            raise ValueError("an entry needs a translation")
        now = time.time() if now is None else now
        key = self.key(source, lang, domain, when_line, when_near, when_window)
        existing = self.entries.get(key)
        created = existing is None
        entry = existing or LibraryEntry(source=source, target=target, lang=lang or None)
        entry.target = target
        entry.lang = lang or None
        entry.pos = pos or None
        entry.domain = domain or None
        entry.note = note or None
        entry.when_line = str(when_line).strip() if when_line else None
        entry.when_near = _as_near(when_near)
        entry.when_window = str(when_window).strip() if when_window else None
        entry.updated = now
        self.entries[key] = entry
        if source in self.suppressed:
            # Writing an entry for a source the user had hidden is an unambiguous
            # statement that they want it back.
            self.suppressed.remove(source)
        self.save()
        self.load()
        return self.entries.get(key, entry), created

    def find(
        self,
        source: str,
        lang: str | None = None,
        domain: str | None = None,
        when_line: object = None,
        when_near: object = (),
        when_window: object = None,
    ) -> LibraryEntry | None:
        """The entry for this source, optionally narrowed to language, scene or conditions.

        With no narrowing and exactly one row for the source, that row is meant; with
        several, None is returned rather than an arbitrary choice, because "delete this
        term" against a term that exists in two languages (or two scenes, or with two sets
        of conditions) has more than one meaning and picking one silently is how the wrong
        row disappears.
        """
        source = source.strip()
        if lang is not None or domain is not None or when_line or when_near or when_window:
            exact = self.entries.get(
                self.key(source, lang, domain, when_line, when_near, when_window)
            )
            if exact is not None:
                return exact
            if when_line or when_near or when_window:
                return None
            # Language and scene but no conditions: "the row for this term in this scene".
            # Unique means unambiguous, so it is returned rather than missed just because
            # that row happens to carry conditions the caller did not restate.
            matches = [
                row
                for row in self.for_source(source)
                if (row.lang or "") == (lang or "") and (row.domain or "") == (domain or "")
            ]
            return matches[0] if len(matches) == 1 else None
        rows = self.for_source(source)
        return rows[0] if len(rows) == 1 else None

    def for_source(self, source: str) -> list[LibraryEntry]:
        source = source.strip()
        return [
            entry
            for (row_source, _lang, _domain, _conditions), entry in self.entries.items()
            if row_source == source
        ]

    def delete(
        self,
        source: str,
        lang: str | None = None,
        domain: str | None = None,
        when_line: object = None,
        when_near: object = (),
        when_window: object = None,
        *,
        conditions_given: bool = False,
    ) -> bool:
        """Remove one entry. For an override this *is* the revert: the shipped entry
        underneath becomes visible again."""
        source = source.strip()
        if conditions_given:
            key = self.key(source, lang, domain, when_line, when_near, when_window)
        elif lang is not None or domain is not None:
            # Language and scene but no conditions: only unambiguous if exactly one row
            # matches them. A same-scene pair told apart by conditions is two rows, and
            # guessing which one was meant is how the wrong meaning disappears.
            matches = [
                row
                for row in self.for_source(source)
                if (row.lang or "") == (lang or "") and (row.domain or "") == (domain or "")
            ]
            if len(matches) != 1:
                return False
            key = self.key(
                matches[0].source,
                matches[0].lang,
                matches[0].domain,
                matches[0].when_line,
                matches[0].when_near,
                matches[0].when_window,
            )
        else:
            rows = self.for_source(source)
            if len(rows) != 1:
                # Zero means nothing to delete; more than one means the caller has to say
                # which language, scene or conditions, and both cases are reported rather
                # than guessed.
                return False
            key = self.key(
                rows[0].source,
                rows[0].lang,
                rows[0].domain,
                rows[0].when_line,
                rows[0].when_near,
                rows[0].when_window,
            )
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
            self.entries.pop(
                self.key(
                    entry.source,
                    entry.lang,
                    entry.domain,
                    entry.when_line,
                    entry.when_near,
                    entry.when_window,
                ),
                None,
            )
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

    def merge(
        self,
        incoming: Iterable[LibraryEntry],
        *,
        replace: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Apply imported entries. Reports what each one did.

        ``replace`` is the only sensible default for an import that is meant to fix
        vocabulary: a file that says what a term should be overrides what was there. The
        alternative (skip existing) is kept for callers that want a purely additive
        import, and both are reported per entry so the user can see which happened.

        ``dry_run`` does everything except touch anything: the same classification, the
        same counts, no write. It is what an import preview needs, and it is implemented
        here rather than in the caller so that the preview cannot disagree with the real
        import -- a preview computed by a second copy of this logic would be a guess about
        the import, not a report from it.
        """
        added, updated, skipped = [], [], []
        now = time.time()
        incoming = list(incoming)
        planned: list[tuple[tuple[str, str, str], LibraryEntry, str]] = []
        for entry in incoming:
            source = entry.source.strip()
            if not source or not entry.target.strip():
                skipped.append(source or "(empty)")
                continue
            key = self.key(
                source,
                entry.lang,
                entry.domain,
                entry.when_line,
                entry.when_near,
                entry.when_window,
            )
            existing = self.entries.get(key)
            if existing is not None and not replace:
                skipped.append(source)
                continue
            planned.append((key, entry, source))
            (updated if existing is not None else added).append(source)
        if not dry_run:
            for key, entry, source in planned:
                entry.source = source
                entry.updated = entry.updated or now
                self.entries[key] = entry
                if source in self.suppressed:
                    self.suppressed.remove(source)
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
        """Write the whole file, grouping every answer for one source together.

        A source answered once is written as a plain object, which is what a hand-written
        corpus looks like. A source answered more than once -- two languages, or one
        language in two scenes -- is written as a *list* of objects, because a JSON object
        cannot have two identical keys, and the failure mode of trying would be silent:
        the file would look right and hold one of them. ``load`` reads that list back.
        """
        payload: dict[str, Any] = {"_readme": _README}
        if self.suppressed:
            payload["_suppress"] = sorted(self.suppressed, key=str.lower)

        grouped: dict[str, list[LibraryEntry]] = {}
        for entry in self.entries.values():
            grouped.setdefault(entry.source, []).append(entry)

        body: dict[str, Any] = {}
        for source in sorted(grouped, key=str.lower):
            rows = sorted(
                grouped[source],
                key=lambda item: (
                    item.lang or "",
                    item.domain or "",
                    item.when_line or "",
                    item.when_near,
                    item.when_window or "",
                ),
            )
            body[source] = rows[0].to_json() if len(rows) == 1 else [r.to_json() for r in rows]
        payload["entries"] = body
        write_json_atomic(self.path, payload)

    # -- introspection ----------------------------------------------------- #

    def describe(self) -> list[dict[str, Any]]:
        return [
            entry.to_dict()
            for entry in sorted(
                self.entries.values(),
                key=lambda e: (
                    e.source.lower(),
                    e.lang or "",
                    e.domain or "",
                    e.when_line or "",
                    e.when_near,
                    e.when_window or "",
                ),
            )
        ]

    @property
    def domains(self) -> list[str]:
        """Every scene the user's own entries declare, for a picker."""
        seen: dict[str, str] = {}
        for entry in self.entries.values():
            key = domain_key(entry.domain)
            if key and key not in seen:
                seen[key] = str(entry.domain).strip()
        return sorted(seen.values(), key=lambda value: value.casefold())

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


def _same_scene(left: str | None, right: str | None) -> bool:
    """Whether two entries belong to the same scene, for picking a row's shadow.

    An entry with no scene is not a rival of one that names a scene: both are live, and
    they answer differently depending on the scene the user selected. Reporting one as
    having overridden the other would tell the user that a row is dead when it is not.
    """
    return domain_key(left) == domain_key(right)


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

    # Keyed the way the edited file is: a source can be answered once per language, once
    # per scene, and once per set of conditions, and the note the user typed belongs to one
    # of those answers, not to the source.
    mine: dict[tuple[Any, ...], LibraryEntry] = {}
    for row_source, row_lang, row_scene, row_conditions in library.entries:
        entry = library.entries[(row_source, row_lang, row_scene, row_conditions)]
        mine[(
            normalize(row_source),
            _language_base(row_lang),
            domain_key(row_scene),
            row_conditions,
        )] = entry

    def engine_conditions(entry: Any) -> tuple[str, tuple[str, ...], str]:
        """The engine's conditions in the editor's identity form, so the two can be matched.

        The engine holds compiled patterns and the editor holds the strings from the file;
        ``translate._compile_condition`` compiles the stripped pattern, so ``.pattern`` is
        the same text the user typed and the keys line up.
        """
        conditions = getattr(entry, "conditions", None)
        if conditions is None:
            return ("", (), "")
        return (
            conditions.line.pattern if conditions.line is not None else "",
            tuple(conditions.near),
            conditions.window.pattern if conditions.window is not None else "",
        )

    def row(entry: Any, *, hidden: bool = False) -> dict[str, Any]:
        key = normalize(entry.source)
        siblings = by_source.get(key, [])
        shadowed = [
            other for other in siblings
            if (other.layer, other.origin, other.lang) != (entry.layer, entry.origin, entry.lang)
            and _same_language(other.lang, entry.lang)
            and _same_scene(other.domain, entry.domain)
        ]
        own = mine.get((
            key,
            _language_base(entry.lang),
            domain_key(entry.domain),
            engine_conditions(entry),
        ))
        return {
            "source": entry.source,
            "target": entry.target,
            "lang": entry.lang or "",
            "domain": entry.domain or "",
            "when_line": (own.when_line or "") if own else "",
            "when_near": ", ".join(own.when_near) if own else "",
            "when_window": (own.when_window or "") if own else "",
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
        #: every scene name in effect anywhere, for the scene picker -- the editor's own
        #: rows plus whatever the shipped corpora declare, so a user can pick a shipped
        #: scene without having to guess its spelling or read the files
        "scenes": sorted(
            {*(library.domains), *((getattr(corpus, "domains", None) or []) if corpus else [])},
            key=lambda value: str(value).casefold(),
        ),
        #: entries that lost to another entry on the same term, language, scene and
        #: conditions. The panel shows the count, because this is the one diagnostic that
        #: tells a user their second meaning is not being used and why.
        "conflicts": list(getattr(corpus, "conflicts", ()) or ()) if corpus is not None else [],
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
            when_line=(values.get("when_line") or "").strip() or None,
            when_near=_as_near(values.get("when_near")),
            when_window=(values.get("when_window") or "").strip() or None,
        ))
    return entries, problems


def entries_from_payload(data: Any) -> tuple[list[LibraryEntry], list[str]]:
    """Turn the JSON corpus forms into entries.

    The same shapes the corpus loader accepts -- a bare mapping, ``{"entries": ...}``, an
    object per entry, or a *list* of objects for one source -- so a file that works as a
    corpus imports without conversion. The list form matters most of all: it is how a term
    with two meanings is written, and rejecting it would make the importer unable to read
    the very shape this feature exists to produce.
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
        for raw in (value if isinstance(value, list) else [value]):
            if isinstance(raw, str):
                entries.append(LibraryEntry(
                    source=source, target=raw, lang=str(file_lang) if file_lang else None
                ))
                continue
            if not isinstance(raw, dict):
                problems.append(f"{source!r}: not a string, an object, or a list of those")
                continue
            target = raw.get("target") or raw.get("translation") or raw.get("text")
            if not isinstance(target, str) or not target.strip():
                problems.append(f"{source!r}: no translation")
                continue
            raw_lang = raw.get("lang") or raw.get("target_lang") or raw.get("language")
            entries.append(LibraryEntry(
                source=source,
                target=target,
                lang=str(raw_lang) if raw_lang else (str(file_lang) if file_lang else None),
                pos=raw.get("pos"),
                domain=raw.get("domain") or raw.get("scene"),
                note=raw.get("note"),
                when_line=str(raw["when_line"]) if raw.get("when_line") else None,
                when_near=_as_near(raw.get("when_near") or raw.get("when_nearby")),
                when_window=str(raw["when_window"]) if raw.get("when_window") else None,
            ))
    if not entries and not problems:
        problems.append("no entries found in the file")
    # Sorted, so an import's report lists what it did in the same order every run: a
    # summary that reshuffles between runs is one nobody trusts.
    entries.sort(key=lambda item: (item.source.lower(), item.lang or "", item.domain or ""))
    return entries, problems


def sniff_format(text: str, fmt: str = "") -> str:
    """Decide which of the known formats this text is, from the text itself.

    Exists so that "auto" means one thing in one place. The panel used to sniff in
    JavaScript and the engine refused ``auto`` outright, which is two answers to one
    question -- and a user with a plain ``.txt`` glossary got neither: the extension is not
    one of the known formats, so the import tried JSON and failed on a two-column list.
    """
    chosen = (fmt or "").strip().lower()
    if chosen in EXPORT_FORMATS:
        return chosen
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return "json"
    first = next((line for line in text.splitlines() if line.strip()), "")
    return "tsv" if "\t" in first else "csv"


def parse_import(text: str, fmt: str) -> tuple[list[LibraryEntry], list[str]]:
    """Parse imported text into entries, or explain why it could not be.

    ``auto`` (and an empty format) sniffs instead of guessing JSON, so a pasted block of
    ``原文,译文`` lines and a hand-written ``.txt`` glossary both import without the caller
    having to know which of the three formats it is looking at.
    """
    if not text.strip():
        return [], ["the file is empty"]
    fmt = sniff_format(text, fmt)
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
    editor's richer one -- and a source answered more than once becomes a *list*, because
    that is the only way one JSON object can carry both and the alternative is an export
    that silently drops half of what the user just exported.
    """
    if fmt in ("csv", "tsv"):
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter="," if fmt == "csv" else "\t", lineterminator="\n")
        writer.writerow(
            ["source", "target", "lang", "pos", "domain", "when_line", "when_near", "when_window", "note"]
        )
        for entry in entries:
            writer.writerow([
                entry.get("source", ""),
                entry.get("target", ""),
                entry.get("lang", ""),
                entry.get("pos", ""),
                entry.get("domain", ""),
                entry.get("when_line", ""),
                entry.get("when_near", ""),
                entry.get("when_window", ""),
                entry.get("note", ""),
            ])
        return buffer.getvalue()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if not entry.get("source") or not entry.get("target"):
            continue
        row = {
            key: entry[key]
            for key in ("target", "lang", "pos", "domain", "when_line", "when_window")
            if entry.get(key)
        }
        near = entry.get("when_near")
        if isinstance(near, str) and near.strip():
            row["when_near"] = [item.strip() for item in near.split(",") if item.strip()]
        elif isinstance(near, (list, tuple)) and near:
            row["when_near"] = list(near)
        grouped.setdefault(str(entry["source"]), []).append(row)

    body: dict[str, Any] = {}
    for source in sorted(grouped, key=str.lower):
        rows = sorted(
            grouped[source], key=lambda item: (str(item.get("lang") or ""), str(item.get("domain") or ""))
        )
        body[source] = rows[0] if len(rows) == 1 else rows

    payload: dict[str, Any] = {"_comment": "Exported by Project Watashi."}
    payload["entries"] = body
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def library_from_corrections(
    corrections: Any,
    *,
    lang: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    """Turn recorded corrections into a corpus payload, for bulk promotion.

    A correction and a corpus entry are different things on purpose -- one is what a human
    said about a line they saw, the other is vocabulary -- but they are the same *shape*
    of statement, and a user who has just corrected thirty lines while watching should not
    have to retype them as entries one at a time. This is the conversion, kept pure so it
    can be previewed before anything is written.

    ``lang`` fills in a language for corrections that declare none, and is written at the
    file level so the result is a valid corpus file rather than a pile of untagged rows.
    """
    body: dict[str, Any] = {}
    for correction in corrections.items():
        if scope is not None and correction.scope != scope:
            continue
        source = str(correction.source or "").strip()
        target = str(correction.target or "").strip()
        if not source or not target:
            continue
        row: dict[str, Any] = {"target": target}
        language = correction.lang or lang
        if language:
            row["lang"] = str(language)
        if correction.note:
            row["note"] = correction.note
        existing = body.get(source)
        if existing is None:
            # The same source corrected for two languages: a list, exactly as the editor
            # writes it, so the promoted file means what the corrections meant.
            body[source] = row
        elif isinstance(existing, list):
            existing.append(row)
        else:
            body[source] = [existing, row]
    payload: dict[str, Any] = {
        "_comment": "Promoted from 实时纠正 by Project Watashi.",
        "entries": body,
    }
    if lang:
        payload["lang"] = str(lang)
    return payload
