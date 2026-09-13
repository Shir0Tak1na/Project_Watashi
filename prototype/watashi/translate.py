"""Corpus and rule based translation engine (requirements R2 / R3).

Design goals, matching the project requirements:

* **Local only** -- nothing here touches the network. Everything is read from
  JSON files on disk.
* **File driven (R2)** -- corpora are plain JSON files. Layered lookup:
  user private > domain (slang / fiction) > general dictionary.
* **Rule inference (R3)** -- words missing from the corpus are handled by
  rules loaded from data files, not hardcoded logic. Rule types:
  ``affix``, ``morpheme``, ``template``, ``transliterate``.
* **Explainable** -- every produced span records where it came from
  (``corpus:user`` / ``rule:affix`` / ``literal``) plus a confidence value, so
  the UI can dim low-confidence output and a human can audit it.

The engine is intentionally token level and greedy: it walks the input string
and always prefers the longest corpus match at each position, which makes
multi-word phrases ("gg wp") win over single words ("gg").
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .correct import Corrections, resolve_corrections_path
from .lang import language_base, same_language, to_nllb_code

# --------------------------------------------------------------------------- #
# layers
# --------------------------------------------------------------------------- #

LAYER_USER = "user"
LAYER_DOMAIN = "domain"
LAYER_GENERAL = "general"

#: lower rank wins when two layers offer the same source term
_LAYER_RANK = {LAYER_USER: 0, LAYER_DOMAIN: 1, LAYER_GENERAL: 2}

#: Keys that declare the language of a whole corpus file. They only mean anything in
#: the ``{"lang": ..., "entries": {...}}`` form, where the top level is metadata.
_FILE_LANGUAGE_KEYS = ("lang", "target_lang", "language")


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Conditions:
    """What has to be true around a term before this entry may be used.

    This is the answer to "one word, several meanings": the meanings cannot be told
    apart by the word, so each one says what distinguishes it. Every condition present
    must hold (a plain AND, because an entry that requires two things at once is the
    common case and ``or`` can be had by writing two entries).

    Patterns are compiled once, at load. Nothing here runs on the hot path unless a
    source actually has more than one candidate -- see ``CorpusStore._select``.
    """

    #: re.search against the whole recognised line
    line: re.Pattern[str] | None = None
    #: any of these appears in the line *outside* the matched term
    near: tuple[str, ...] = ()
    #: re.search against the window title the text was read from
    window: re.Pattern[str] | None = None

    @property
    def empty(self) -> bool:
        return self.line is None and not self.near and self.window is None

    def key(self) -> tuple[str, tuple[str, ...], str]:
        """A hashable identity for these conditions.

        Part of what makes two entries rivals: two meanings of one term distinguished only
        by their conditions are two entries, not one entry and a duplicate. Without this
        the second one landed on the same key as the first and was discarded -- which made
        the whole conditional mechanism unusable for the case it exists for, one word with
        two meanings in one scene.
        """
        return (
            self.line.pattern if self.line is not None else "",
            self.near,
            self.window.pattern if self.window is not None else "",
        )

    def describe(self) -> str:
        parts = []
        if self.line is not None:
            parts.append(f"when_line={self.line.pattern!r}")
        if self.near:
            parts.append(f"when_near={list(self.near)!r}")
        if self.window is not None:
            parts.append(f"when_window={self.window.pattern!r}")
        return ", ".join(parts)


@dataclass(frozen=True)
class Context:
    """Everything a lookup is allowed to depend on besides the text itself.

    Deliberately tiny and comparable, because it is also the *cache* key: whatever a
    translation may depend on has to be in here, or two situations that deserve
    different answers will share one cached answer. ``CorpusStore.context_key`` decides
    which parts actually go into a key, and it drops the ones the loaded vocabulary
    cannot distinguish -- so a corpus with no scene tags and no window conditions
    behaves exactly as it did before this existed.
    """

    #: target language; "" means "use the caller's argument"
    target_lang: str = ""
    #: the scene/domain the user selected, matched against ``Entry.domain``
    scene: str = ""
    #: title of the window the text was read from, when the capture source knows one
    window: str = ""

    def effective_target(self, fallback: str = "") -> str:
        """The target to translate into: this context's, or the caller's."""
        return self.target_lang or fallback

    def with_target(self, target_lang: str) -> "Context":
        if self.target_lang == target_lang:
            return self
        return Context(target_lang=target_lang, scene=self.scene, window=self.window)

    def with_scene(self, scene: str) -> "Context":
        return Context(target_lang=self.target_lang, scene=scene, window=self.window)


#: The context every caller gets when it does not pass one: a target language and
#: nothing else. Kept as one object so the common case allocates nothing.
DEFAULT_CONTEXT = Context()


@dataclass(frozen=True)
class Entry:
    """One corpus entry.

    ``lang`` is the language the *target* is written in, and it is not optional
    bookkeeping: without it a corpus is language-blind, so an English->Chinese
    vocabulary answers a request for Japanese with Chinese, at full confidence. The
    engine cannot infer the language of a translation it is handed -- only whoever
    wrote the entry knows -- so an entry that does not say is treated as usable for
    every target, which is exactly the behaviour every existing corpus file has.

    ``domain`` is the other half of that idea, one level down: it names the *scene*
    the entry belongs to, and it is how one corpus answers a word two ways ("bank" in
    a finance screen, "bank" by a river) without the two entries destroying each
    other. Which one wins is decided at lookup by ``Context.scene``; see ``_rank``.
    """

    source: str
    target: str
    layer: str
    origin: str  # e.g. "corpus:slang"
    priority: int = 0
    pos: str | None = None
    domain: str | None = None
    #: language of ``target``: "zh-CN", "ja", "en", or an NLLB code. None = any.
    lang: str | None = None
    #: what has to hold around the term for this entry to be the right one; None means
    #: "no conditions", which is every entry written before this field existed.
    #: ``compare=False`` so two entries that differ only in a compiled pattern are
    #: still equal -- equality here means "the same vocabulary row", not "the same
    #: regex object".
    conditions: Conditions | None = field(default=None, compare=False)

    def sense(self) -> str:
        """A short label for which meaning of a term this entry is."""
        bits = [self.domain or ""]
        if self.conditions is not None and not self.conditions.empty:
            bits.append(self.conditions.describe())
        return " · ".join(bit for bit in bits if bit)

    def slot(self) -> tuple[str, str, str, tuple[str, tuple[str, ...], str]]:
        """What makes two entries rivals: same term, language, scene **and conditions**.

        Conditions belong here, and leaving them out was a real defect: a term whose two
        meanings are told apart purely by ``when_line``/``when_near``/``when_window`` --
        which is the case the conditions exist for -- collapsed onto one key at load and
        lost one meaning, in silence. Two entries that agree on all four are genuinely the
        same statement said twice, and that is the collision worth reporting.
        """
        return (
            _language_base(self.lang),
            normalize(self.source),
            _domain_key(self.domain),
            self.conditions.key() if self.conditions is not None else ("", (), ""),
        )


@dataclass
class Span:
    """A translated slice of the input, with provenance."""

    source: str
    target: str
    origin: str
    confidence: float
    rule_id: str | None = None

    @property
    def explained(self) -> bool:
        """True when the span came from the corpus, a rule, or a human correction."""
        return self.origin.startswith(("corpus:", "rule:", "correction:"))


@dataclass
class Outcome:
    """Translation result for one recognised line."""

    source_text: str
    target_text: str
    spans: list[Span] = field(default_factory=list)
    backend: str = "corpus+rules"

    @property
    def coverage(self) -> float:
        """Share of source characters explained by corpus or rules (0..1)."""
        total = sum(len(s.source) for s in self.spans if s.source.strip())
        if total == 0:
            return 0.0
        explained = sum(
            len(s.source) for s in self.spans if s.source.strip() and s.explained
        )
        return explained / total

    @property
    def confidence(self) -> float:
        if not self.spans:
            return 0.0
        return sum(s.confidence for s in self.spans) / len(self.spans)

    def trace(self) -> str:
        """Human readable provenance, for logs and the debug panel."""
        parts = []
        for s in self.spans:
            if not s.source.strip():
                continue
            tag = s.rule_id or s.origin
            parts.append(f"{s.source!r}->{s.target!r}[{tag} {s.confidence:.2f}]")
        return " ".join(parts)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def normalize(text: str) -> str:
    """Length preserving lowercasing.

    ``str.lower()`` can change length (e.g. ``'İ'``), which would break the
    index arithmetic used for span scanning, so only ASCII A-Z is folded.
    """
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in text)


_LATIN = re.compile(r"[0-9A-Za-z]")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
#: latin words, or *runs* of CJK (which has no spaces to tokenise on)
_TOKEN = re.compile(
    r"[A-Za-z][A-Za-z'\u2019\-]*|[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+"
)


def _is_word_char(ch: str) -> bool:
    return bool(_LATIN.match(ch) or _CJK.match(ch))


def _load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[translate] skipping {path}: {exc}")
        return None


def _coerce_entries(
    source: str,
    value: Any,
    layer: str,
    origin: str,
    default_lang: str | None = None,
) -> list[Entry]:
    """Every entry one corpus value can describe.

    A value may be a string, an object, or a **list of objects** -- the last one so that a
    single file can answer the same term differently per target language. It has to be a
    list rather than repeated keys, because a JSON object with two identical keys is not
    a thing: whichever parser you use silently keeps one of them, and the file would look
    correct while losing half its content.
    """
    if isinstance(value, list):
        entries: list[Entry] = []
        for item in value:
            entries.extend(_coerce_entries(source, item, layer, origin, default_lang))
        return entries
    entry = _coerce_entry(source, value, layer, origin, default_lang=default_lang)
    return [entry] if entry is not None else []


def _coerce_entry(
    source: str,
    value: Any,
    layer: str,
    origin: str,
    default_lang: str | None = None,
) -> Entry | None:
    """Accept both ``{"word": "译"}`` and the richer object form.

    ``default_lang`` is the language declared by the file as a whole, so a corpus that
    is entirely one language says so once instead of on every entry. An entry may still
    override it, which is what makes a mixed file possible.
    """
    if isinstance(value, str):
        return Entry(
            source=source, target=value, layer=layer, origin=origin, lang=default_lang
        )
    if isinstance(value, dict):
        target = (
            value.get("target")
            or value.get("translation")
            or value.get("text")
        )
        if not isinstance(target, str) or not target:
            return None
        raw_lang = value.get("lang") or value.get("target_lang") or value.get("language")
        return Entry(
            source=source,
            target=target,
            layer=layer,
            origin=origin,
            priority=int(value.get("priority", 0) or 0),
            pos=value.get("pos"),
            domain=value.get("domain"),
            lang=str(raw_lang) if raw_lang else default_lang,
            conditions=_conditions_from(value, origin),
        )
    return None


def _language_base(tag: str | None) -> str:
    """The language part of a tag or NLLB code. Moved to ``lang`` so the corrections
    file and the library editor share it rather than keeping three copies of the rule
    that must agree; kept under this name because callers and the editor import it
    from here."""
    return language_base(tag)


def _domain_key(value: str | None) -> str:
    """The comparison key for a scene/domain name: trimmed, case-folded.

    ``Finance`` and ``finance`` are the same scene, and a stray space in a hand-edited
    file must not create a second one that never matches.
    """
    return (value or "").strip().casefold()


def _compile_condition(pattern: Any, what: str, origin: str) -> re.Pattern[str] | None:
    """Compile one condition pattern, complaining instead of raising.

    A single bad regex in a shipped corpus must not take the engine down, and it must
    not silently disable the *entry* either: the entry is dropped with its reason
    printed, which is the same treatment rules and corpus files already get.
    """
    if pattern is None:
        return None
    if not isinstance(pattern, str) or not pattern.strip():
        print(f"[translate] {origin}: {what} needs a non-empty pattern, ignored")
        return None
    try:
        # Compiled from the *stripped* pattern, so that ``Conditions.key()`` -- which the
        # editor matches its rows by -- is the same string the file holds and the user
        # typed. A trailing space that changed the key but not the match would make the
        # editor show a row with no conditions while the engine applied them.
        return re.compile(pattern.strip(), re.IGNORECASE)
    except re.error as exc:
        print(f"[translate] {origin}: {what} is not a valid regex ({exc}), ignored")
        return None


def _conditions_from(value: dict[str, Any], origin: str) -> Conditions | None:
    """Build the conditions an entry declares, or None when it declares none.

    Unknown keys are left alone: the whole corpus format tolerates extra keys (``pos``
    and ``domain`` sat unused for several releases), and refusing to load a file
    because of one would be a worse failure than ignoring it.
    """
    line = _compile_condition(value.get("when_line"), "when_line", origin)
    window = _compile_condition(value.get("when_window"), "when_window", origin)
    raw_near = value.get("when_near") or value.get("when_nearby")
    near: list[str] = []
    if isinstance(raw_near, str):
        near = [item.strip() for item in raw_near.split(",") if item.strip()]
    elif isinstance(raw_near, (list, tuple)):
        near = [str(item).strip() for item in raw_near if str(item).strip()]
    elif raw_near is not None:
        print(f"[translate] {origin}: when_near must be a string or a list, ignored")
    conditions = Conditions(line=line, near=tuple(near), window=window)
    return None if conditions.empty else conditions


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #


class Rule:
    """Base class for inference rules applied to words missing from the corpus."""

    kind = "base"

    def __init__(self, spec: dict[str, Any], origin: str) -> None:
        self.spec = spec
        self.origin = origin
        self.id: str = spec.get("id") or f"{self.kind}-{origin}"
        self.enabled: bool = bool(spec.get("enabled", True))
        # lower priority value == tried earlier
        self.priority: int = int(spec.get("priority", 100))
        self.confidence: float = float(spec.get("confidence", 0.4))
        # optional language filter, e.g. "zh-CN"
        self.target: str | None = spec.get("target")

    def matches_language(self, target_lang: str) -> bool:
        """Does this rule apply when translating into ``target_lang``?

        Compared as a *language*, not as a string. A rule tagged ``zh-CN`` must apply
        when the user asks for ``zh``, ``zho_Hans`` or ``zh_CN`` -- they are the same
        language, and every one of those spellings is a tag this project accepts
        elsewhere (``lang.NLLB_CODES``, the NLLB passthrough, the settings field). An
        equality test here meant that typing ``zh`` instead of ``zh-CN`` silently
        switched off every Chinese rule in the shipped rule set, with no diagnostic:
        the rule engine simply looked broken.
        """
        if self.target is None:
            return True
        return same_language(self.target, target_lang)

    def apply(self, token: str, corpus: "CorpusStore", target_lang: str) -> str | None:
        raise NotImplementedError


class AffixRule(Rule):
    """Strip a known prefix/suffix, translate the stem, re-attach the affix."""

    kind = "affix"

    def __init__(self, spec: dict[str, Any], origin: str) -> None:
        super().__init__(spec, origin)
        self.prefixes: dict[str, str] = {
            normalize(k): v for k, v in (spec.get("prefixes") or {}).items()
        }
        self.suffixes: dict[str, str] = {
            normalize(k): v for k, v in (spec.get("suffixes") or {}).items()
        }
        self.min_stem = int(spec.get("min_stem", 2))

    def apply(self, token: str, corpus: "CorpusStore", target_lang: str) -> str | None:
        low = normalize(token)

        # prefix: <affix gloss><translated stem>
        for affix in sorted(self.prefixes, key=len, reverse=True):
            if not low.startswith(affix):
                continue
            stem = token[len(affix) :]
            if len(stem) < self.min_stem:
                continue
            hit = corpus.lookup_exact(stem, target_lang)
            if hit is None:
                continue
            return f"{self.prefixes[affix]}{hit.target}"

        # suffix: <translated stem><affix gloss>
        for affix in sorted(self.suffixes, key=len, reverse=True):
            if not low.endswith(affix):
                continue
            stem = token[: len(token) - len(affix)]
            if len(stem) < self.min_stem:
                continue
            hit = corpus.lookup_exact(stem, target_lang)
            if hit is None:
                continue
            return f"{hit.target}{self.suffixes[affix]}"

        return None


class MorphemeRule(Rule):
    """Split an unknown word into known corpus morphemes and recombine them."""

    kind = "morpheme"

    def __init__(self, spec: dict[str, Any], origin: str) -> None:
        super().__init__(spec, origin)
        self.separator: str = spec.get("separator", "")

    def apply(self, token: str, corpus: "CorpusStore", target_lang: str) -> str | None:
        low = normalize(token)
        pieces: list[str] = []
        i = 0
        while i < len(low):
            hit = None
            for end in range(len(low), i, -1):
                candidate = low[i:end]
                hit = corpus.lookup_exact(candidate, target_lang)
                if hit is not None:
                    pieces.append(hit.target)
                    i = end
                    break
            if hit is None:
                return None
        if len(pieces) < 2:
            return None
        return self.separator.join(pieces)


class TemplateRule(Rule):
    """Apply a naming-convention regex template, e.g. ``(.+)境`` -> ``\\1 Realm``."""

    kind = "template"

    def __init__(self, spec: dict[str, Any], origin: str) -> None:
        super().__init__(spec, origin)
        pattern = spec.get("pattern")
        self.replacement: str = spec.get("replacement", "")
        self.direction: str = str(spec.get("direction", "source")).lower()
        try:
            self.regex = re.compile(pattern) if pattern else None
        except re.error as exc:
            print(f"[translate] bad regex in rule {self.id}: {exc}")
            self.regex = None
        if self.regex is None:
            # A template with no usable pattern can never match anything, so it is
            # disabled rather than left in the rule list. Keeping it made the loaded
            # rule count -- which the UI shows and a user reads as "these rules are
            # working" -- include a rule that cannot fire.
            self.enabled = False

    def apply(self, token: str, corpus: "CorpusStore", target_lang: str) -> str | None:
        if self.regex is None:
            return None
        match = self.regex.fullmatch(token)
        if match is None:
            return None
        groups = {k: v for k, v in match.groupdict().items() if v is not None}
        if not groups:
            return self.replacement or None
        # translate each captured group through the corpus when possible
        resolved: dict[str, str] = {}
        for name, value in groups.items():
            hit = corpus.lookup_exact(value, target_lang)
            resolved[name] = hit.target if hit else value
        try:
            return self.replacement.format(**resolved)
        except (KeyError, IndexError):
            return None


class TransliterateRule(Rule):
    """Last-resort fallback: keep the token as-is, flagged low confidence.

    A real implementation would render the token into the target script's
    phonology; for now the identity mapping keeps proper nouns readable while
    making it obvious nothing better matched.
    """

    kind = "transliterate"

    def apply(self, token: str, corpus: "CorpusStore", target_lang: str) -> str | None:
        if not token.strip():
            return None
        return token


_RULE_TYPES: dict[str, type[Rule]] = {
    AffixRule.kind: AffixRule,
    MorphemeRule.kind: MorphemeRule,
    TemplateRule.kind: TemplateRule,
    TransliterateRule.kind: TransliterateRule,
}


def _is_cjk_target(lang: str) -> bool:
    return lang.lower().startswith(("zh", "ja", "ko"))


# --------------------------------------------------------------------------- #
# corpus
# --------------------------------------------------------------------------- #


class CorpusStore:
    """Layered corpus with mtime based hot reload.

    ``lookup_exact`` is used by both the span scanner and the rules, so a rule
    that decomposes a word still benefits from user vocabulary.

    Hot reload is checked from ``translate`` rather than from a watcher thread: the
    engine already calls ``translate`` once per recognised line, an mtime stat is
    microseconds against the ~20 ms of work that follows, and a watcher would need a
    thread, a shutdown path and a way to tell the pipeline that its vocabulary moved
    underneath it. The check is throttled so a burst of frames does not stat the tree
    once per frame.
    """

    def __init__(
        self,
        layers: dict[str, Sequence[Path]] | None = None,
        rule_files: Sequence[Path] = (),
        auto_reload: bool = True,
        reload_interval_s: float = 0.5,
        corrections: "Corrections | None" = None,
    ) -> None:
        self._layers: dict[str, list[Path]] = {
            LAYER_USER: [],
            LAYER_DOMAIN: [],
            LAYER_GENERAL: [],
        }
        for layer, paths in (layers or {}).items():
            self._layers.setdefault(layer, []).extend(Path(p) for p in paths)

        self._entries: dict[tuple[Any, ...], Entry] = {}
        self._max_key = 1
        #: language base -> (entries usable for that target, longest key). Built at
        #: load rather than filtered per frame: this is the hot path, called once per
        #: recognised line, and it must not walk the corpus to answer a question that
        #: only changes when the files do.
        self._views: dict[str, tuple[dict[str, tuple[Entry, ...]], int]] = {}
        #: language bases any entry declares, for diagnostics: "why is nothing being
        #: translated?" is almost always "the corpus is for a different target"
        self._languages: list[str] = []
        self._has_untagged = False
        #: entries that lost to another entry on the same (source, language, scene).
        #: Kept so the number is visible instead of being the silence it used to be.
        self._conflicts: list[dict[str, Any]] = []
        #: what this vocabulary can distinguish, so a cache key knows which parts of a
        #: context to include and which would only split the cache for nothing
        self._uses_domains = False
        self._uses_windows = False
        self._uses_conditions = False
        #: normalized sources the user layer asked to hide, and the entries they hid --
        #: kept for introspection so an editor can list what is hidden and bring it back
        self._suppressed: set[str] = set()
        self._hidden_entries: list[Entry] = []
        self._rules: list[Rule] = []
        self._rule_files: list[Path] = [Path(p) for p in rule_files]
        self._mtimes: dict[Path, tuple[float, int]] = {}
        self._lock = threading.RLock()
        self._auto_reload = auto_reload
        self._reload_interval = max(0.0, float(reload_interval_s))
        #: -inf, not 0.0: the first lookup after a start has to check, and a clock
        #: that happens to be near zero would otherwise swallow that first check
        self._last_check = float("-inf")
        self.reloads = 0
        #: Bumped every time the loaded vocabulary is replaced. A background model
        #: refinement started before a change describes a vocabulary that no longer
        #: exists, so whoever queued it can tell that its answer is out of date --
        #: which is how a correction stops being overwritten by a refinement that was
        #: already in flight when the user made it.
        self.revision = 0
        #: human corrections, consulted before the corpus scan; see watashi/correct.py
        self.corrections: Corrections | None = corrections
        if self.corrections is None and self._layers[LAYER_USER]:
            # Derived from the user layer rather than passed in, so an engine built by
            # a benchmark or a self check reads the same corrections the application
            # does instead of quietly ignoring them.
            self.corrections = Corrections(
                resolve_corrections_path(list(self._layers[LAYER_USER]))
            )
        self.load()

    @property
    def auto_reload(self) -> bool:
        return self._auto_reload

    @auto_reload.setter
    def auto_reload(self, value: Any) -> None:
        self._auto_reload = bool(value)

    @property
    def reload_interval_s(self) -> float:
        return self._reload_interval

    @reload_interval_s.setter
    def reload_interval_s(self, value: Any) -> None:
        self._reload_interval = max(0.0, float(value))
        # a shorter interval must not wait out the longer one that was just replaced
        self._last_check = float("-inf")

    # -- loading ---------------------------------------------------------- #

    def _iter_corpus_files(self, layer: str) -> Iterable[Path]:
        for root in self._layers.get(layer, []):
            if root.is_file() and root.suffix == ".json":
                yield root
            elif root.is_dir():
                yield from sorted(root.rglob("*.json"))

    def _snapshot_mtimes(self) -> dict[Path, tuple[float, int]]:
        """Every corpus and rule file, with the modification time *and its size*.

        The size is not redundant. NTFS timestamps come from a system clock that ticks
        every ~15.6 ms, so two writes inside one tick get the *identical* mtime -- and a
        file edit that lands in the same tick as the previous one is then invisible to
        mtime comparison alone. That is not hypothetical: it made ``selfcheck_correct``
        fail about one run in ten, with an edit that had plainly happened on disk and a
        reload counter of zero. Size catches the common case (an edit that changes the
        length); a same-tick, same-length edit remains undetectable this way, which is
        why an explicit reload exists and why a correction forces one.
        """
        seen: dict[Path, tuple[float, int]] = {}
        for layer in self._layers:
            for path in self._iter_corpus_files(layer):
                try:
                    info = path.stat()
                except OSError:
                    continue
                seen[path] = (info.st_mtime, info.st_size)
        for path in self._rule_files:
            try:
                info = path.stat()
            except OSError:
                continue
            seen[path] = (info.st_mtime, info.st_size)
        if self.corrections is not None:
            # normally inside a corpus directory and so already counted; listed again
            # because a corrections file that is not is the one that would be missed
            try:
                info = self.corrections.path.stat()
                seen[self.corrections.path] = (info.st_mtime, info.st_size)
            except OSError:
                pass
        return seen

    def reload_if_changed(self, now: float | None = None, force: bool = False) -> bool:
        """Reload when any corpus or rule file changed on disk.

        Throttled, because the caller is the translate path: ``force`` is for the
        cases that must be synchronous -- a reload button, or a correction the user
        is about to judge by what appears on screen a moment later.
        """
        if not self._auto_reload and not force:
            return False
        now = time.monotonic() if now is None else now
        if not force:
            if now - self._last_check < self._reload_interval:
                return False
            self._last_check = now
        current = self._snapshot_mtimes()
        with self._lock:
            if current == self._mtimes:
                return False
        self.load()
        self.reloads += 1
        return True

    def load(self) -> None:
        # Keyed by (language, source, scene): two entries for the same source word in
        # different target languages are not duplicates, they are the two answers this
        # project exists to keep apart; and two entries for the same source in two
        # *scenes* are the two meanings of one word, which is the other thing that must
        # not destroy itself. Keying on the source alone discarded whichever came
        # second, silently, which made both of those impossible to express at all.
        entries: dict[tuple[Any, ...], Entry] = {}
        #: entries that lost to another entry on the same key: not an error, but not
        #: nothing either -- see ``_conflict``
        conflicts: list[dict[str, Any]] = []
        rule_specs: list[tuple[dict[str, Any], str]] = []
        #: sources the user has hidden. Only the user layer may say this -- suppression is
        #: the user's statement about the shipped corpora, and a shipped file that
        #: suppressed entries would be shipping a hole. Collected as the first layer is
        #: walked, which is what makes it apply to the ones below.
        suppressed: set[str] = set()
        hidden_entries: list[Entry] = []

        for layer in (LAYER_USER, LAYER_DOMAIN, LAYER_GENERAL):
            for path in self._iter_corpus_files(layer):
                data = _load_json(path)
                if not isinstance(data, dict):
                    continue
                origin = f"corpus:{path.stem}"
                # allow {"entries": {...}} as well as a bare mapping
                wrapped = isinstance(data.get("entries"), dict)
                body = data["entries"] if wrapped else data
                if layer == LAYER_USER:
                    raw_suppress = data.get("_suppress")
                    if isinstance(raw_suppress, list):
                        suppressed.update(
                            normalize(str(item))
                            for item in raw_suppress
                            if str(item).strip()
                        )
                # A file may declare the language of everything in it. Only in the
                # wrapped form, where the top level is already metadata: in the bare
                # form a key is an entry, and the English word "lang" is a source term
                # someone could legitimately be translating.
                file_lang: str | None = None
                if wrapped:
                    raw_lang = (
                        data.get("lang") or data.get("target_lang") or data.get("language")
                    )
                    file_lang = str(raw_lang) if raw_lang else None
                elif any(k in data for k in _FILE_LANGUAGE_KEYS):
                    # In the bare form a key *is* an entry, so "lang" here would become a
                    # term called "lang" whose translation is "zh-CN" -- silently, and
                    # the file's language would stay unset. Said out loud rather than
                    # guessed at: reading the intent would mean either losing a
                    # legitimate entry for the English word "lang" or inventing a rule
                    # about which values "look like" a language.
                    print(
                        f"[translate] {path.name}: a file-level language needs the "
                        '{"lang": ..., "entries": {...}} form; ignoring the bare '
                        f'"lang" key, which is being read as an entry'
                    )
                for key, value in body.items():
                    if key.startswith("_") or not isinstance(key, str) or not key.strip():
                        continue
                    for entry in _coerce_entries(
                        key, value, layer, origin, default_lang=file_lang
                    ):
                        # "I do not want this shipped entry" -- kept out of every lookup,
                        # and kept in a list of its own so an editor can still show it and
                        # offer to bring it back. Dropping it silently would leave the user
                        # with a row that vanished and no way to ask why.
                        if layer != LAYER_USER and normalize(key) in suppressed:
                            hidden_entries.append(entry)
                            continue
                        slot = entry.slot()
                        existing = entries.get(slot)
                        if existing is None:
                            entries[slot] = entry
                        elif _better(entry, existing):
                            entries[slot] = entry
                            if existing.target != entry.target:
                                # Only a collision that *changes an answer* is worth
                                # reporting. Two entries that agree on the translation are
                                # the same statement written twice: true, harmless, and
                                # noise in a list the user is meant to act on.
                                conflicts.append(
                                    _conflict(existing, entry, "replaced by a higher layer")
                                )
                        elif entry.target != existing.target:
                            conflicts.append(
                                _conflict(entry, existing, "ignored in favour of a higher layer")
                            )

        for path in self._rule_files:
            data = _load_json(path)
            if not isinstance(data, dict):
                continue
            for spec in data.get("rules", []) or []:
                if isinstance(spec, dict):
                    rule_specs.append((spec, path.stem))

        rules: list[Rule] = []
        for spec, origin in rule_specs:
            cls = _RULE_TYPES.get(str(spec.get("type", "")).lower())
            if cls is None:
                print(f"[translate] unknown rule type {spec.get('type')!r}, skipped")
                continue
            try:
                rule = cls(spec, origin)
            except Exception as exc:  # a bad rule must not kill the engine
                print(f"[translate] failed to build rule {spec.get('id')!r}: {exc}")
                continue
            if rule.enabled:
                rules.append(rule)
        rules.sort(key=lambda r: r.priority)

        with self._lock:
            self._entries = entries
            self._max_key = max((len(slot[1]) for slot in entries), default=1)
            self._rules = rules
            self._mtimes = self._snapshot_mtimes()
            self._views = self._build_views(entries)
            self._languages = sorted({slot[0] for slot in entries if slot[0]})
            self._has_untagged = any(not slot[0] for slot in entries)
            self._suppressed = suppressed
            self._hidden_entries = hidden_entries
            self._conflicts = conflicts
            #: What this vocabulary can distinguish, which is what a cache key has to
            #: include. A corpus that names no scene and tests no window cannot answer
            #: differently per scene or per window, so its caches must not be split by
            #: them -- splitting would throw away hits for no gain.
            self._uses_domains = any(_domain_key(entry.domain) for entry in entries.values())
            self._uses_windows = any(
                entry.conditions is not None and entry.conditions.window is not None
                for entry in entries.values()
            )
            self._uses_conditions = any(
                entry.conditions is not None for entry in entries.values()
            )
            self.revision += 1
        if conflicts:
            # Said out loud, once, with the first few named. Loading used to keep one
            # entry and drop the rest in complete silence, so "I wrote both meanings and
            # only one works" had no diagnostic anywhere in the program.
            examples = "; ".join(
                f"{item['source']!r} kept {item['kept']['target']!r} "
                f"({item['kept']['origin']}), dropped {item['dropped']['target']!r} "
                f"({item['dropped']['origin']})"
                for item in conflicts[:3]
            )
            print(
                f"[translate] {len(conflicts)} entr"
                f"{'y' if len(conflicts) == 1 else 'ies'} lost to another entry on the same "
                f"source, language and scene: {examples}"
                + (" ..." if len(conflicts) > 3 else "")
                + "  (give them different domains, or add when_* conditions, to keep both)"
            )
        if self.corrections is not None:
            # one reload path for the whole vocabulary: a corpus file and a correction
            # arrive through the same call, so there is no way to get one without the other
            self.corrections.load()

    @staticmethod
    def _build_views(
        entries: dict[tuple[Any, ...], Entry]
    ) -> dict[str, tuple[dict[str, tuple[Entry, ...]], int]]:
        """One view per declared language: source -> the senses that could answer it.

        ``""`` is the view of entries that declare no language, which are usable for
        every target. Each language's view is that plus its own entries.

        A *tuple of candidates* rather than one winner, because the winner is not a
        property of the file: it depends on the scene the user selected, on the line the
        term was found in and on the window it came from. Picking at load time meant
        picking without the one thing the choice is about. The merge is still by layer
        precedence and still a loop rather than two dict updates -- an untagged entry and
        a language-tagged one are different slots, so a plain merge would let the tagged
        one win regardless of layer and an untagged user override would silently lose to
        the shipped entry it was written to replace.

        Each list is sorted into the neutral-context order once, here, so a lookup in
        the default context does no sorting at all.
        """
        untagged: dict[str, list[Entry]] = {}
        for slot, entry in entries.items():
            if not slot[0]:
                untagged.setdefault(slot[1], []).append(entry)
        views: dict[str, tuple[dict[str, tuple[Entry, ...]], int]] = {}
        bases = {slot[0] for slot in entries if slot[0]}
        for base in bases:
            merged: dict[str, list[Entry]] = {
                norm: list(items) for norm, items in untagged.items()
            }
            for slot, entry in entries.items():
                if slot[0] != base:
                    continue
                merged.setdefault(slot[1], []).append(entry)
            views[base] = _freeze_view(merged)
        views[""] = _freeze_view(untagged)
        return views

    def view_for(
        self, target_lang: str | None
    ) -> tuple[dict[str, tuple[Entry, ...]], int]:
        """The entries usable for this target, and the longest source key among them."""
        base = _language_base(target_lang)
        with self._lock:
            if base in self._views:
                return self._views[base]
            # No entry declares this language, so only the language-neutral ones apply.
            # Falling back is deliberate: a corpus that has never heard of languages
            # keeps working exactly as it did before this dimension existed.
            return self._views.get("", ({}, 1))

    # -- queries ---------------------------------------------------------- #

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def languages(self) -> list[str]:
        """The target languages entries actually declare, for diagnostics.

        The honest answer to "why is nothing being translated?": almost always because
        the corpus is written for a different target than the one selected.
        """
        return list(self._languages)

    @property
    def has_untagged_entries(self) -> bool:
        return self._has_untagged

    @property
    def suppressed_sources(self) -> list[str]:
        """What the user has hidden from the shipped corpora."""
        return sorted(self._suppressed)

    @property
    def conflicts(self) -> list[dict[str, Any]]:
        """Entries that lost to another entry on the same source, language and scene.

        Not an error: the winner is defined and deterministic. It is a *diagnostic*,
        and the one that was missing while one word with two meanings looked like a
        corpus that had loaded cleanly.
        """
        return [dict(item) for item in self._conflicts]

    @property
    def domains(self) -> list[str]:
        """Every scene name the loaded entries declare, sorted, without blanks.

        For the panel's scene picker and for the engine's own diagnostics: "which
        scenes exist?" is otherwise only answerable by grepping the corpus files, and
        typing a scene name that matches nothing is silently a no-op.
        """
        seen: dict[str, str] = {}
        for entry in self._entries.values():
            key = _domain_key(entry.domain)
            if key and key not in seen:
                seen[key] = str(entry.domain).strip()
        return sorted(seen.values(), key=lambda value: value.casefold())

    def correction_stats(self) -> dict[str, Any]:
        """What the corrections file is doing: how many rows, how many hits, how many
        of them declare no language.

        The last one is the surprising one and therefore the one worth publishing: a
        correction with no language applies under *every* target, which is the right
        default for files written before the field existed and a confusing thing to
        discover by watching a Chinese correction answer a Japanese request.
        """
        store = self.corrections
        if store is None:
            return {"corrections": 0, "corrections_lines": 0, "corrections_hits": 0,
                    "corrections_untagged": 0, "corrections_file": ""}
        return dict(store.stats())

    def context_key(self, context: Context | None = None) -> tuple[str, ...]:
        """The part of a context this vocabulary can actually distinguish.

        Every cache in the engine keys on this, which is how the two failure modes are
        closed at once. A cache keyed on the source text alone serves a Chinese line to
        a request for Japanese; a cache keyed on the *whole* context splits into
        misses for dimensions the corpus cannot see. So the key carries exactly the
        dimensions the loaded vocabulary is sensitive to: always the target language,
        the scene if any entry names one, the window if any entry tests one.

        A corpus written before any of this existed has neither, so its key is the
        target language and its behaviour is what it always was.
        """
        context = context or DEFAULT_CONTEXT
        key = [context.target_lang]
        if self._uses_domains:
            key.append(_domain_key(context.scene))
        if self._uses_windows:
            key.append(context.window.strip())
        return tuple(key)

    def senses(self, source: str, target_lang: str | None = None) -> list[Entry]:
        """Every entry that could answer this source, best-first for a neutral context.

        For the editor and the panel: "why did it pick that one" and "what else did I
        write for this term" are the same question, and it cannot be answered from a
        view that only exposes the winner.
        """
        entries, _max = self.view_for(target_lang)
        return list(entries.get(normalize(source), ()))

    def hidden_entries(self) -> list[Entry]:
        """The entries that suppression is keeping out of the lookup.

        Returned rather than discarded because the alternative is an editor showing a row
        that quietly disappeared, with no way to ask why or to undo it.
        """
        with self._lock:
            return list(self._hidden_entries)

    def language_summary(self) -> str:
        """A one-line description of what the corpus can actually answer for."""
        parts = list(self._languages)
        if self._has_untagged:
            parts.append("未标注(任意目标)")
        return "、".join(parts) if parts else "空"

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def rule_ids(self) -> list[str]:
        return [r.id for r in self._rules]

    def entries_snapshot(self) -> list[Entry]:
        """A stable copy of the loaded entries, for listings and the web panel."""
        with self._lock:
            return sorted(
                self._entries.values(), key=lambda e: (e.layer, e.lang or "", e.source)
            )

    def lookup_exact(
        self,
        term: str,
        target_lang: str | None = None,
        context: Context | None = None,
    ) -> Entry | None:
        """The entry for ``term``, restricted to ``target_lang`` when one is given.

        Without a language the language-neutral entries win, and failing that the
        answer is deterministic (the first language, in sorted order) rather than
        whichever happened to be loaded last -- a corpus that answers differently
        between runs is worse than one that answers imperfectly.

        With a context, the same selection the translate path uses applies: the scene
        and the conditions decide between senses, and a term is no longer assumed to
        have one answer.
        """
        if not term:
            return None
        norm = normalize(term)
        context = context or DEFAULT_CONTEXT
        if target_lang is not None:
            context = context.with_target(target_lang)
        if context.target_lang:
            entries, _max = self.view_for(context.target_lang)
            return self._pick(entries.get(norm, ()), context, term, 0, len(term))
        # No target language asked for: the neutral view first, then the first language
        # in sorted order that can answer, which is the old behaviour and stays
        # deterministic.
        with self._lock:
            untagged = self._views.get("", ({}, 1))[0]
            languages = list(self._languages)
        hit = self._pick(untagged.get(norm, ()), context, term, 0, len(term))
        if hit is not None:
            return hit
        for base in languages:
            entries, _max = self.view_for(base)
            hit = self._pick(entries.get(norm, ()), context, term, 0, len(term))
            if hit is not None:
                return hit
        return None

    @staticmethod
    def _pick(
        candidates: Sequence[Entry],
        context: Context,
        text: str,
        start: int,
        length: int,
    ) -> Entry | None:
        """``_select`` over a possibly-missing key, with the lock already held or not
        needed: kept separate so both the scan and ``lookup_exact`` select identically.
        """
        if not candidates:
            return None
        return _select(candidates, context, text, start, length)

    # -- translation ------------------------------------------------------ #

    def translate(
        self,
        text: str,
        target_lang: str = "zh-CN",
        context: Context | None = None,
    ) -> Outcome:
        """Translate one line using longest-match corpus spans then rules.

        ``context`` carries what the answer may depend on besides the text: the target
        language, the selected scene, and the window the text came from. It is optional
        and defaults to the target language alone, so every existing caller keeps its
        behaviour and a corpus with no scene tags and no conditions cannot tell the
        difference.
        """
        if not text.strip():
            return Outcome(source_text=text, target_text=text)

        # A corpus edit on disk -- including one this project just made from a user
        # correction -- applies here, at the next line, rather than at the next start.
        self.reload_if_changed()

        context = (context or DEFAULT_CONTEXT).with_target(target_lang)
        effective = context.effective_target(target_lang)
        context = context.with_target(effective)

        # A whole-line correction is checked first and outranks everything: the human
        # saw this exact sentence come out wrong and said what it should say. It is
        # returned as a single span so the rest of the engine sees it as one fully
        # explained, fully confident hit -- which is also what stops the local model
        # from being asked to improve a sentence a person has already settled.
        if self.corrections is not None:
            correction = self.corrections.lookup_line(text, effective)
            if correction is not None:
                span = Span(
                    source=text,
                    target=correction.target,
                    origin="correction:user",
                    confidence=1.0,
                    rule_id=f"corrected:{correction.scope}",
                )
                return Outcome(
                    source_text=text,
                    target_text=correction.target,
                    spans=[span],
                    backend="corpus+rules",
                )

        with self._lock:
            rules = list(self._rules)
        # The entries usable for *this* target. An English->Chinese vocabulary must not
        # answer a request for Japanese: without this the user reads a third language
        # back at full confidence, which is the same failure the echo gate catches for
        # source == target and is harder to notice, because the text is foreign either
        # way.
        entries, max_key = self.view_for(effective)

        norm = normalize(text)
        spans: list[Span] = []
        i = 0
        n = len(norm)
        pending_start = 0

        def flush_pending(end: int) -> None:
            """Emit unmatched text, applying rules to its word tokens."""
            if end <= pending_start:
                return
            chunk = text[pending_start:end]
            pos = 0
            for match in _TOKEN.finditer(chunk):
                if match.start() > pos:
                    spans.append(
                        Span(chunk[pos:match.start()], chunk[pos:match.start()], "literal", 0.0)
                    )
                token = match.group(0)
                # ``entries`` is deliberately not passed: rules work on tokens the
                # corpus scan left *unmatched*, and they re-enter the corpus through
                # ``lookup_exact`` for the stem they strip.
                translated, used_rule, conf = self._apply_rules(token, rules, effective)
                spans.append(
                    Span(
                        source=token,
                        target=translated,
                        origin=f"rule:{used_rule.kind}" if used_rule else "literal",
                        confidence=conf,
                        rule_id=used_rule.id if used_rule else None,
                    )
                )
                pos = match.end()
            if pos < len(chunk):
                spans.append(Span(chunk[pos:], chunk[pos:], "literal", 0.0))

        while i < n:
            matched: tuple[int, Entry] | None = None
            upper = min(max_key, n - i)
            for length in range(upper, 0, -1):
                candidate = norm[i : i + length]
                # One dict get, then the same boundary guards as before. The senses for
                # this key are already sorted best-first for a neutral context, so the
                # ordinary case is candidates[0] and costs one length comparison; only a
                # key with several senses, or one with conditions, does more.
                entry = _select(entries.get(candidate, ()), context, text, i, length)
                if entry is None:
                    continue
                # don't match in the middle of a latin word
                if _LATIN.match(candidate[0]) and i > 0 and _is_word_char(text[i - 1]):
                    continue
                if _LATIN.match(candidate[-1]) and i + length < n and _is_word_char(text[i + length]):
                    continue
                matched = (length, entry)
                break

            if matched is None:
                i += 1
                continue

            length, entry = matched
            flush_pending(i)
            spans.append(
                Span(
                    source=text[i : i + length],
                    target=entry.target,
                    origin=entry.origin,
                    confidence=1.0 if entry.layer == LAYER_USER else 0.95,
                )
            )
            i += length
            pending_start = i

        flush_pending(n)

        target_text = _join_spans(spans, effective)
        return Outcome(source_text=text, target_text=target_text, spans=spans)

    def _apply_rules(
        self,
        token: str,
        rules: list[Rule],
        target_lang: str,
    ) -> tuple[str, Rule | None, float]:
        for rule in rules:
            if not rule.matches_language(target_lang):
                continue
            try:
                result = rule.apply(token, self, target_lang)
            except Exception as exc:  # never let one rule break a frame
                print(f"[translate] rule {rule.id} failed on {token!r}: {exc}")
                continue
            if result:
                return result, rule, rule.confidence
        return token, None, 0.0


def _freeze_view(
    merged: dict[str, list[Entry]]
) -> tuple[dict[str, tuple[Entry, ...]], int]:
    """Sort each source's senses into their neutral-context order and freeze them.

    Sorting here rather than per lookup is the whole reason the hot path can stay a
    dict get: the order is the same for every line until a scene or a condition enters
    the picture, and when one does, ``_select`` only has to compare ranks -- it never
    sorts.
    """
    frozen = {
        norm: tuple(sorted(items, key=lambda entry: _rank(entry, DEFAULT_CONTEXT)))
        for norm, items in merged.items()
    }
    return frozen, max((len(key) for key in frozen), default=1)


def _better(candidate: Entry, incumbent: Entry) -> bool:
    """Pick the winning entry for a duplicate source term."""
    cand_rank = (_LAYER_RANK.get(candidate.layer, 9), -candidate.priority)
    inc_rank = (_LAYER_RANK.get(incumbent.layer, 9), -incumbent.priority)
    return cand_rank < inc_rank


def _conflict(loser: Entry, winner: Entry, why: str) -> dict[str, Any]:
    """A record of one entry that lost to another on the same key.

    Recorded rather than printed and forgotten, because the interesting question is
    not "which one won" (that is defined and deterministic) but "how many of my
    entries are not being used, and why" -- and before this existed the answer was
    silence. The user's attempt to write one word two ways used to look exactly like
    a corpus that had loaded cleanly.
    """
    return {
        "source": loser.source,
        "lang": loser.lang or "",
        "domain": loser.domain or "",
        "kept": {
            "target": winner.target,
            "origin": winner.origin,
            "layer": winner.layer,
            "priority": winner.priority,
        },
        "dropped": {
            "target": loser.target,
            "origin": loser.origin,
            "layer": loser.layer,
            "priority": loser.priority,
        },
        "why": why,
    }


def _scene_rank(entry: Entry, context: Context) -> int:
    """How well an entry fits the scene the user selected. Lower is better.

    ``0`` exact, ``1`` the entry names no scene (so it is usable anywhere), ``2`` a
    different scene. With no scene selected everything is ``0``, which is what makes
    the whole dimension inert until the user asks for it.
    """
    if not context.scene:
        return 0
    key = _domain_key(entry.domain)
    if not key:
        return 1
    return 0 if key == _domain_key(context.scene) else 2


def _rank(entry: Entry, context: Context) -> tuple[Any, ...]:
    """Ordering for entries that compete for the same term. Lower wins.

    Layer comes **first**, and that ordering is the point: the user's own file is their
    word on the matter, and a scene tag on a shipped entry must not override it. Below
    the layer, the scene decides; then the entry's own priority; then a deterministic
    tail so the answer cannot depend on which file was read first.
    """
    return (
        _LAYER_RANK.get(entry.layer, 9),
        _scene_rank(entry, context),
        -entry.priority,
        entry.origin,
        _domain_key(entry.domain),
        entry.target,
    )


def _near_words(text: str, start: int, length: int) -> str:
    """The line with the matched term removed, for ``when_near`` to look at.

    Cut out rather than skipped over, because "bank" must not count as being near
    itself: ``when_near: ["bank"]`` on an entry for "bank" would otherwise be true of
    every line containing it, which is a condition that cannot fail and therefore a
    condition the author did not mean to write.
    """
    return text[:start] + " " + text[start + length :]


def _conditions_hold(
    conditions: Conditions,
    context: Context,
    text: str,
    start: int,
    length: int,
) -> bool:
    """Whether every condition an entry declares holds for this occurrence."""
    if conditions.line is not None and not conditions.line.search(text):
        return False
    if conditions.window is not None:
        # No window known (a fixed region, a synthetic source, a benchmark) means the
        # condition cannot be checked. It is treated as not holding, so an entry that
        # asked for a window is not applied to text that did not come from one: the
        # alternative is a scene-specific term leaking into every other scene.
        if not context.window:
            return False
        if not conditions.window.search(context.window):
            return False
    if conditions.near:
        rest = _near_words(text, start, length).casefold()
        if not any(word.casefold() in rest for word in conditions.near):
            return False
    return True


def _select(
    candidates: Sequence[Entry],
    context: Context,
    text: str,
    start: int,
    length: int,
) -> Entry | None:
    """The entry to use for one matched span, out of the senses that share its key.

    The single-candidate case is the one that matters for latency and it does no work
    beyond one comparison: no conditions to evaluate, no ranking, no allocation.
    Everything else -- several senses, conditions, a selected scene -- only happens
    when a corpus actually asks for it.
    """
    if len(candidates) == 1:
        only = candidates[0]
        if only.conditions is None or _conditions_hold(
            only.conditions, context, text, start, length
        ):
            return only
        return None
    best: Entry | None = None
    for entry in candidates:
        if entry.conditions is not None and not _conditions_hold(
            entry.conditions, context, text, start, length
        ):
            continue
        if best is None or _rank(entry, context) < _rank(best, context):
            best = entry
    return best


def _join_spans(spans: Sequence[Span], target_lang: str) -> str:
    """Concatenate span targets.

    Unmatched source runs are emitted as ``literal`` spans that carry the
    original whitespace, so plain concatenation reproduces the source spacing
    exactly -- including the boundary between two independently translated
    words. Nothing is squeezed or inserted: predictable spacing beats clever
    spacing when a human is reading subtitles.
    """
    text = "".join(span.target for span in spans if span.target)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


# --------------------------------------------------------------------------- #
# backend protocol
# --------------------------------------------------------------------------- #


class Translator:
    """Pluggable translation backend."""

    name = "base"

    def translate(
        self,
        text: str,
        target_lang: str = "zh-CN",
        context: Context | None = None,
    ) -> Outcome:
        raise NotImplementedError

    def stats(self) -> dict[str, Any]:
        return {}


class CorpusTranslator(Translator):
    """Corpus + rules only. Fully local, no model weights required."""

    name = "corpus+rules"

    def __init__(self, corpus: CorpusStore) -> None:
        self.corpus = corpus
        self._cache: dict[tuple[str, ...], Outcome] = {}
        self._hits = 0
        self._misses = 0

    def translate(
        self,
        text: str,
        target_lang: str = "zh-CN",
        context: Context | None = None,
    ) -> Outcome:
        # The key is what the corpus says it can distinguish, not the source text
        # alone: a cache keyed on the text serves the wrong language's answer, and one
        # keyed on everything the caller knows splits into misses for dimensions this
        # vocabulary cannot see.
        context = (context or DEFAULT_CONTEXT).with_target(target_lang)
        key = (text, *self.corpus.context_key(context))
        cached = self._cache.get(key)
        if cached is not None:
            self._hits += 1
            return cached
        self._misses += 1
        outcome = self.corpus.translate(text, context.effective_target(target_lang), context)
        if len(self._cache) > 4096:
            self._cache.clear()
        self._cache[key] = outcome
        return outcome

    def stats(self) -> dict[str, Any]:
        total = self._hits + self._misses
        return {
            "backend": self.name,
            "corpus_entries": self.corpus.size,
            "corpus_languages": self.corpus.language_summary(),
            "rules": self.corpus.rule_count,
            "rule_ids": self.corpus.rule_ids(),
            "cache_hit_rate": (self._hits / total) if total else 0.0,
            # Published so a surface can say *why* a correction stopped applying, and how
            # many of them behave that way. ``Corrections.stats()`` existed and was called
            # by nobody, which made "the engine reports untagged corrections" a claim with
            # no outlet -- the same shape of half-truth as an entry nothing reads.
            **self.corpus.correction_stats(),
        }


class LocalLlmTranslator(Translator):
    """Optional local LLM backend (requirement R1 / M3).

    Activates only when ``llama_cpp`` is importable and a model path is
    configured; otherwise it falls back to the corpus engine so the pipeline
    always works. No cloud call is ever made.
    """

    name = "local-llm"

    def __init__(
        self,
        model_path: str | None,
        fallback: Translator,
        n_ctx: int = 2048,
        n_threads: int | None = None,
    ) -> None:
        self.fallback = fallback
        self.model_path = model_path
        self._llm = None
        self._error: str | None = None
        if not model_path:
            self._error = "no model path configured"
            return
        try:
            from llama_cpp import Llama  # type: ignore
        except Exception as exc:
            self._error = f"llama_cpp not installed ({exc})"
            return
        try:
            self._llm = Llama(
                model_path=model_path,
                n_ctx=n_ctx,
                n_threads=n_threads,
                verbose=False,
            )
        except Exception as exc:
            self._error = f"failed to load model: {exc}"

    @property
    def available(self) -> bool:
        return self._llm is not None

    def translate(
        self,
        text: str,
        target_lang: str = "zh-CN",
        context: Context | None = None,
    ) -> Outcome:
        if self._llm is None:
            return self.fallback.translate(text, target_lang, context)
        prompt = (
            "Translate the following text into "
            f"{target_lang}. Keep proper nouns and invented terms consistent. "
            f"Output only the translation.\n\n{text}\n"
        )
        try:
            result = self._llm(prompt, max_tokens=256, temperature=0.1, stop=["\n\n"])
            out = result["choices"][0]["text"].strip()
        except Exception as exc:
            print(f"[translate] local LLM failed: {exc}")
            return self.fallback.translate(text, target_lang, context)
        return Outcome(source_text=text, target_text=out, backend=self.name)

    def stats(self) -> dict[str, Any]:
        base = dict(self.fallback.stats())
        base.update(
            {
                "backend": self.name if self.available else f"{self.name} (fallback)",
                "llm_available": self.available,
                "llm_error": self._error,
            }
        )
        return base


# --------------------------------------------------------------------------- #
# convenience
# --------------------------------------------------------------------------- #


def load_translator(
    user_dirs: Sequence[Path] = (),
    domain_dirs: Sequence[Path] = (),
    general_dirs: Sequence[Path] = (),
    rule_files: Sequence[Path] = (),
    llm_model: str | None = None,
    auto_reload: bool = True,
) -> Translator:
    """Build the corpus store and wrap it in the requested backend."""
    corpus = CorpusStore(
        layers={
            LAYER_USER: list(user_dirs),
            LAYER_DOMAIN: list(domain_dirs),
            LAYER_GENERAL: list(general_dirs),
        },
        rule_files=list(rule_files),
        auto_reload=auto_reload,
    )
    base = CorpusTranslator(corpus)
    if llm_model:
        return LocalLlmTranslator(llm_model, base)
    return base
