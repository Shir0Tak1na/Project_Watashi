"""Recording a human correction so it sticks (requirement R2, in use).

The engine is deliberately two-tiered: the corpus is authoritative and the model
only fills the grammar around it. That design has a hole in it, and this module is
the patch. When the corpus is *wrong* -- a name romanised the wrong way, a skill
mistranslated, a line whose subject the model guessed backwards -- no amount of
extra vocabulary helps, because the wrong answer is not a gap, it is a confident
hit. The only thing that can outrank an authoritative wrong answer is a human.

So: the user reads a bad translation on screen, types the right one, and it is
written into the user corpus layer and reloaded. Three properties matter, and each
of them is a way this feature can be quietly broken:

* **It must be a file.** Corrections land in ``corrections.json`` in the user
  corpus layer, which is where a corpus belongs: inspectable, diffable, editable
  by hand, and portable to another machine. A correction kept only in a running
  process would be lost at the moment the user has finished doing the work.
* **It must not clobber.** A machine written file is written whole, atomically,
  and never merged into a hand written one. Rewriting a file the user maintains
  would destroy exactly the material they care about.
* **It must take effect now.** Hence the loose whole-line key below: OCR does not
  return the same string twice, so a correction keyed only on the exact source
  string would apply to the frame it was made from and to nothing after it.

The distinction between the two scopes is not cosmetic. A **line** correction is
keyed on the whole sentence and matched loosely, which is what the user wants when
a sentence was translated wrongly. A **term** correction is keyed exactly and
matched by the ordinary corpus scan wherever the term appears, which is the robust
one: it survives the sentence around it being read differently, so it is the right
choice for a name that appears on every screen.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .lang import language_base
from .recent import MIN_KEY_LENGTH, normalize as loose_normalize

#: written into the user corpus layer, so the ordinary corpus loader picks it up
CORRECTIONS_FILE = "corrections.json"

#: the whole recognised line maps to this translation
SCOPE_LINE = "line"
#: the term maps to this translation wherever it appears
SCOPE_TERM = "term"
SCOPES = (SCOPE_LINE, SCOPE_TERM)

_README = (
    "Written by the 实时纠正 feature: screen text a human corrected by hand. "
    "Loaded as the user corpus layer, so it outranks the domain and general "
    "corpora. Safe to edit or delete; unknown keys are ignored."
)

#: one process, one lock; concurrent writers would otherwise lose each other's entries
_WRITE_LOCK = threading.RLock()


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write a whole JSON file, atomically.

    A half written JSON file is a corpus that fails to load, and it fails at the next
    start, far away from the write that caused it. So: temp file, then a rename, which is
    atomic on both POSIX and Windows.

    Shared by every file this application writes on the user's behalf -- corrections and
    the edited library both use it, so neither can half-write its way into a state the
    loader refuses, and there is one place to get the temp-file dance right.
    """
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path = Path(path)
    with _WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)


def resolve_corrections_path(dirs: list[Path], fallback: Path | None = None) -> Path:
    """The corrections file for a list of already resolved user layer paths.

    Shared with the engine, which knows its own layer paths but not the config, so
    the two cannot disagree about where corrections live -- a correction written
    somewhere the engine never reads is the failure mode this avoids.
    """
    for path in dirs:
        if path.is_dir():
            return path / CORRECTIONS_FILE
    for path in dirs:
        if path.suffix == ".json":
            # configured as a single corpus file: corrections go beside it, never into it
            return path.parent / CORRECTIONS_FILE
    if dirs:
        return dirs[0] / CORRECTIONS_FILE
    if fallback is not None:
        return fallback / CORRECTIONS_FILE
    return Path(".") / CORRECTIONS_FILE


def corrections_dir(config: Any) -> Path:
    """Where corrections belong: the user corpus layer, created if it is missing.

    The shipped default points at a directory that does not exist in a fresh
    checkout, which means the top layer of the corpus resolves to nothing at all
    until something creates it. This is the something.
    """
    candidates: list[Path] = []
    resolver = getattr(config, "corpus_dirs", None)
    if callable(resolver):
        try:
            candidates = [Path(p) for p in resolver("user")]
        except Exception:
            candidates = []
    if not candidates:
        raw = (getattr(config, "corpus", None) or {}).get("user") or []
        if isinstance(raw, str):
            raw = [raw]
        base = Path(getattr(config, "base_dir", "."))
        for item in raw:
            path = Path(str(item))
            candidates.append(path if path.is_absolute() else base / path)

    fallback = Path(getattr(config, "base_dir", ".")) / "corpus_user"
    return resolve_corrections_path(candidates, fallback).parent


def corrections_path(config: Any) -> Path:
    return corrections_dir(config) / CORRECTIONS_FILE


@dataclass
class Correction:
    """One human correction, as stored."""

    source: str
    target: str
    scope: str = SCOPE_LINE
    count: int = 1
    first_seen: float = 0.0
    last_seen: float = 0.0
    note: str | None = None
    #: the target language this correction was written in, when it is known.
    #:
    #: Without it a whole-line correction is applied at *any* target, so a Chinese
    #: correction typed while translating into Chinese keeps winning after the user
    #: switches to Japanese -- and the user reads Chinese text they asked not to get,
    #: labelled as Japanese. Same failure the corpus documents for untagged entries, and
    #: the same answer: an untagged correction is language-neutral (which is what every
    #: pre-0.0.6a corrections file is, so it keeps working), a tagged one only applies to
    #: that language, and a tagged one wins over a neutral one.
    lang: str | None = None
    #: how often this run actually applied it; the one number that says whether the
    #: correction is doing anything rather than merely being stored
    hits: int = 0

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "target": self.target,
            "scope": self.scope,
            "count": self.count,
            "first_seen": round(self.first_seen, 3),
            "last_seen": round(self.last_seen, 3),
            "note": self.note,
        }
        if self.lang:
            data["lang"] = self.lang
        return data

    def to_dict(self) -> dict[str, Any]:
        return {**self.to_json(), "source": self.source, "hits": self.hits}


def _better_correction(candidate: Correction, incumbent: Correction) -> bool:
    """Pick the winner when two stored corrections land on the same loose key.

    They collide when two exact source strings fold to one loose key -- the same
    sentence stored once with a trailing ``。`` and once without, which is exactly what
    the loose key exists to catch. The most recently touched one wins: corrections are
    human edits, and the newer edit is the human's current opinion. ``count`` breaks a
    tie, then the target text, so the answer cannot depend on file order.
    """
    return (
        candidate.last_seen,
        candidate.count,
        candidate.target,
    ) > (
        incumbent.last_seen,
        incumbent.count,
        incumbent.target,
    )


class Corrections:
    """The corrections file, loaded, with the two lookup keys built from it."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        #: keyed by ``(exact source, language base)``. The language is in the key
        #: because a correction is written *in* a language: keying on the source alone
        #: meant correcting the same line while translating into a second language
        #: overwrote the first correction instead of sitting beside it.
        self._items: dict[tuple[str, str], Correction] = {}
        self._lines: dict[tuple[str, str], Correction] = {}
        self.load()

    # -- loading ----------------------------------------------------------- #

    def load(self) -> None:
        items: dict[tuple[str, str], Correction] = {}
        data: Any = None
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                # A hand edit with a stray comma must not take the engine down; the
                # corrections are lost for this run and said so out loud.
                print(f"[correct] ignoring {self.path}: {exc}")
                data = None

        if isinstance(data, dict):
            body = data.get("entries") if isinstance(data.get("entries"), dict) else data
            for key, value in body.items():
                if not isinstance(key, str) or not key.strip() or key.startswith("_"):
                    continue
                # One correction for a source, or a *list* of them when the same line is
                # corrected differently for two target languages. Same shape the corpus
                # uses for a term answered in two languages, and for the same reason: a
                # JSON object cannot hold two identical keys, so the alternative would be
                # a file that looks right and silently keeps one of them.
                values = value if isinstance(value, list) else [value]
                for raw in values:
                    correction = self._parse(key, raw)
                    if correction is None:
                        continue
                    slot = (key, language_base(correction.lang))
                    existing = items.get(slot)
                    if existing is None or _better_correction(correction, existing):
                        items[slot] = correction

        lines: dict[tuple[str, str], Correction] = {}
        for correction in items.values():
            if correction.scope != SCOPE_LINE:
                continue
            key = loose_normalize(correction.source)
            # Below this length the loose key collides: "是" and "好" would fold onto
            # each other's sentences. Those stay in the corpus layer, where matching
            # is exact and a collision is impossible.
            if len(key) >= MIN_KEY_LENGTH:
                # Keyed by language as well as text, so one line can be corrected
                # differently for two targets.
                slot = (key, language_base(correction.lang))
                existing = lines.get(slot)
                if existing is None or _better_correction(correction, existing):
                    lines[slot] = correction

        self._items = items
        self._lines = lines

    @staticmethod
    def _parse(source: str, value: Any) -> Correction | None:
        if isinstance(value, str):
            return Correction(source=source, target=value)
        if not isinstance(value, dict):
            return None
        target = value.get("target") or value.get("translation") or value.get("text")
        if not isinstance(target, str) or not target.strip():
            return None
        scope = str(value.get("scope") or SCOPE_LINE).strip().lower()
        if scope not in SCOPES:
            scope = SCOPE_LINE
        raw_lang = value.get("lang") or value.get("target_lang") or value.get("language")
        return Correction(
            source=source,
            target=target,
            scope=scope,
            count=int(value.get("count", 1) or 1),
            first_seen=float(value.get("first_seen", 0.0) or 0.0),
            last_seen=float(value.get("last_seen", 0.0) or 0.0),
            note=value.get("note"),
            lang=str(raw_lang) if raw_lang else None,
        )

    # -- lookup ------------------------------------------------------------ #

    def lookup_line(self, text: str, target_lang: str | None = None) -> Correction | None:
        """The whole-line correction for this text, if there is one.

        Checked before the corpus scan, and deliberately loose about whitespace and
        edge punctuation: those are the differences OCR produces between two frames
        of the same sentence, and a correction that only matched the frame it was
        typed from would be useless a second later.

        ``target_lang`` is honoured when given: a correction written for Chinese is not
        the answer to a request for Japanese. A correction with no language is
        neutral and still applies -- that is what every corrections file written before
        the field existed contains, and refusing them would silently discard the user's
        own work on upgrade.
        """
        key = loose_normalize(text)
        if len(key) < MIN_KEY_LENGTH:
            return None
        correction = self._find_line(key, target_lang)
        if correction is not None:
            correction.hits += 1
        return correction

    def _find_line(self, key: str, target_lang: str | None) -> Correction | None:
        if target_lang:
            # the language asked for first, then the neutral one
            for base in (language_base(target_lang), ""):
                hit = self._lines.get((key, base))
                if hit is not None:
                    return hit
            return None
        # No language given: neutral first, then a deterministic pick among languages
        # rather than whichever the file happened to list last.
        neutral = self._lines.get((key, ""))
        if neutral is not None:
            return neutral
        for slot in sorted(self._lines):
            if slot[0] == key:
                return self._lines[slot]
        return None

    def untagged_corrections(self) -> list[Correction]:
        """Corrections that declare no language, in any scope.

        They apply under **every** target, which is a reasonable default and a surprising
        one to discover: a correction typed while translating into Chinese keeps winning
        after a switch to Japanese. Counted here so a surface can say how many behave that
        way -- and counted across *both* scopes, because that is what "declares no
        language" means. Counting only the whole-line ones (which is what this did) gave a
        number that disagreed with the rows a panel marks as untagged, and two answers to
        one question is how a diagnostic stops being trusted.
        """
        return [c for c in self.items() if not c.lang]

    def untagged_line_corrections(self) -> list[Correction]:
        """Whole-line corrections that declare no language.

        The subset that is reachable by loose matching, listed separately because that is
        the subset a correction actually *engages* through -- a term correction with no
        language is still language-neutral vocabulary, and the panel labels both.
        """
        return [
            correction
            for (key, base), correction in sorted(self._lines.items())
            if not base and len(key) >= MIN_KEY_LENGTH
        ]

    def lookup_exact(self, source: str, target_lang: str | None = None) -> Correction | None:
        """One stored correction by its exact source text.

        Language-aware for the same reason ``lookup_line`` is: with two languages'
        corrections for one line, "the correction for this source" is only a well-formed
        question once the language is known. Without one, the neutral row wins, then a
        deterministic pick.
        """
        key = source.strip()
        if target_lang:
            for base in (language_base(target_lang), ""):
                hit = self._items.get((key, base))
                if hit is not None:
                    return hit
            return None
        neutral = self._items.get((key, ""))
        if neutral is not None:
            return neutral
        for slot in sorted(self._items):
            if slot[0] == key:
                return self._items[slot]
        return None

    # -- editing ----------------------------------------------------------- #

    def record(
        self,
        source: str,
        target: str,
        scope: str = SCOPE_LINE,
        note: str | None = None,
        now: float | None = None,
        lang: str | None = None,
    ) -> tuple[Correction, bool]:
        """Store a correction and write the file. Returns ``(correction, created)``.

        Correcting the same line twice updates it rather than adding a rival entry:
        the human's latest answer is the one they mean, and a fork between two rows
        for one sentence would be unresolvable by the engine.

        ``lang`` is the target language this correction was written in, and it is part
        of what "the same line twice" means: correcting one line for Chinese and then
        for Japanese is two corrections, not an edit of the first.
        """
        source = source.strip()
        target = target.strip()
        if not source:
            raise ValueError("a correction needs the source text")
        if not target:
            raise ValueError("a correction needs the corrected translation")
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SCOPES)}, not {scope!r}")

        now = time.time() if now is None else now
        slot = (source, language_base(lang))
        existing = self._items.get(slot)
        created = existing is None
        if existing is None:
            correction = Correction(
                source=source,
                target=target,
                scope=scope,
                count=1,
                first_seen=now,
                last_seen=now,
                note=note,
                lang=str(lang) if lang else None,
            )
        else:
            correction = existing
            correction.target = target
            correction.scope = scope
            correction.last_seen = now
            correction.count += 1
            if note is not None:
                correction.note = note
            if lang:
                correction.lang = str(lang)

        self._items[slot] = correction
        self.save()
        self.load()  # rebuild the loose keys from what was just written
        return correction, created

    def apply(
        self,
        source: str,
        target: str,
        scope: str = SCOPE_LINE,
        note: str | None = None,
        lang: str | None = None,
    ) -> dict[str, Any]:
        """Record and return the summary a surface wants to show."""
        correction, created = self.record(
            source, target, scope=scope, note=note, lang=lang
        )
        return {
            "created": created,
            "updated": not created,
            "source": correction.source,
            "target": correction.target,
            "lang": correction.lang or "",
            "path": str(self.path),
            "scope": correction.scope,
            "count": correction.count,
            "total": len(self),
            "applies_to_future_frames": True,
        }

    def remove(self, source: str, lang: str | None = None) -> bool:
        """Delete one correction, so a mistake about a mistake is reversible.

        With a language, that language's row goes; without one, every row for this
        source goes. Both are deliberate: the listing shows one row per language, so a
        delete aimed at a row must not take its sibling with it, while a delete that
        names only the text means "this sentence is done being corrected".
        """
        key = source.strip()
        if lang is not None:
            doomed = [(key, language_base(lang))]
        else:
            doomed = [slot for slot in self._items if slot[0] == key]
        if not any(slot in self._items for slot in doomed):
            return False
        for slot in doomed:
            self._items.pop(slot, None)
        self.save()
        self.load()
        return True

    def clear(self) -> int:
        count = len(self._items)
        self._items.clear()
        self.save()
        self.load()
        return count

    def save(self) -> None:
        """Write the whole file atomically.

        Grouped by source, and a source with corrections in more than one language is
        written as a *list* -- the shape the loader reads back, and the only way a JSON
        object can hold the same key twice.
        """
        by_source: dict[str, list[Correction]] = {}
        for (source, _base), correction in self._items.items():
            by_source.setdefault(source, []).append(correction)
        entries: dict[str, Any] = {}
        for source, rows in by_source.items():
            rows.sort(key=lambda c: language_base(c.lang))
            entries[source] = rows[0].to_json() if len(rows) == 1 else [c.to_json() for c in rows]
        write_json_atomic(self.path, {"_readme": _README, "entries": entries})

    # -- introspection ----------------------------------------------------- #

    def items(self) -> list[Correction]:
        return sorted(self._items.values(), key=lambda c: (c.scope, c.source, c.lang or ""))

    def term_pairs(self) -> list[tuple[str, str]]:
        """``(source, target)`` for term scope, or everything, for listings."""
        return [(c.source, c.target) for c in self.items()]

    def describe(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self.items()]

    def stats(self) -> dict[str, Any]:
        return {
            "corrections": len(self._items),
            "corrections_lines": len(self._lines),
            "corrections_hits": sum(c.hits for c in self._items.values()),
            #: every correction that declares no language, both scopes -- see
            #: ``untagged_corrections`` for why this is not the whole-line subset
            "corrections_untagged": len(self.untagged_corrections()),
            "corrections_untagged_lines": len(self.untagged_line_corrections()),
            "corrections_file": str(self.path),
        }

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, source: object) -> bool:
        return isinstance(source, str) and any(
            slot[0] == source.strip() for slot in self._items
        )


# --------------------------------------------------------------------------- #
# config level helpers
#
# The engine holds a long lived Corrections object; a surface making a one-off
# change builds one, writes, and drops it. Both end up in the same file, and the
# engine's copy notices through the ordinary corpus reload.
# --------------------------------------------------------------------------- #


def record(
    config: Any,
    source: str,
    target: str,
    scope: str = SCOPE_LINE,
    note: str | None = None,
    lang: str | None = None,
) -> dict[str, Any]:
    return Corrections(corrections_path(config)).apply(
        source, target, scope=scope, note=note, lang=lang
    )


def listing(config: Any) -> list[dict[str, Any]]:
    return Corrections(corrections_path(config)).describe()


def remove(config: Any, source: str, lang: str | None = None) -> bool:
    return Corrections(corrections_path(config)).remove(source, lang=lang)
