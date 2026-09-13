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

# --------------------------------------------------------------------------- #
# layers
# --------------------------------------------------------------------------- #

LAYER_USER = "user"
LAYER_DOMAIN = "domain"
LAYER_GENERAL = "general"

#: lower rank wins when two layers offer the same source term
_LAYER_RANK = {LAYER_USER: 0, LAYER_DOMAIN: 1, LAYER_GENERAL: 2}


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Entry:
    """One corpus entry."""

    source: str
    target: str
    layer: str
    origin: str  # e.g. "corpus:slang"
    priority: int = 0
    pos: str | None = None
    domain: str | None = None


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


def _coerce_entry(source: str, value: Any, layer: str, origin: str) -> Entry | None:
    """Accept both ``{"word": "译"}`` and the richer object form."""
    if isinstance(value, str):
        return Entry(source=source, target=value, layer=layer, origin=origin)
    if isinstance(value, dict):
        target = (
            value.get("target")
            or value.get("translation")
            or value.get("text")
        )
        if not isinstance(target, str) or not target:
            return None
        return Entry(
            source=source,
            target=target,
            layer=layer,
            origin=origin,
            priority=int(value.get("priority", 0) or 0),
            pos=value.get("pos"),
            domain=value.get("domain"),
        )
    return None


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
        return self.target is None or self.target == target_lang

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
            hit = corpus.lookup_exact(stem)
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
            hit = corpus.lookup_exact(stem)
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
                hit = corpus.lookup_exact(candidate)
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
            hit = corpus.lookup_exact(value)
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

        self._entries: dict[str, Entry] = {}
        self._max_key = 1
        self._rules: list[Rule] = []
        self._rule_files: list[Path] = [Path(p) for p in rule_files]
        self._mtimes: dict[Path, float] = {}
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

    def _snapshot_mtimes(self) -> dict[Path, float]:
        seen: dict[Path, float] = {}
        for layer in self._layers:
            for path in self._iter_corpus_files(layer):
                try:
                    seen[path] = path.stat().st_mtime
                except OSError:
                    continue
        for path in self._rule_files:
            try:
                seen[path] = path.stat().st_mtime
            except OSError:
                continue
        if self.corrections is not None:
            # normally inside a corpus directory and so already counted; listed again
            # because a corrections file that is not is the one that would be missed
            try:
                seen[self.corrections.path] = self.corrections.path.stat().st_mtime
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
        entries: dict[str, Entry] = {}
        rule_specs: list[tuple[dict[str, Any], str]] = []

        for layer in (LAYER_USER, LAYER_DOMAIN, LAYER_GENERAL):
            for path in self._iter_corpus_files(layer):
                data = _load_json(path)
                if not isinstance(data, dict):
                    continue
                origin = f"corpus:{path.stem}"
                # allow {"entries": {...}} as well as a bare mapping
                body = data.get("entries") if isinstance(data.get("entries"), dict) else data
                for key, value in body.items():
                    if key.startswith("_") or not isinstance(key, str) or not key.strip():
                        continue
                    entry = _coerce_entry(key, value, layer, origin)
                    if entry is None:
                        continue
                    norm = normalize(key)
                    existing = entries.get(norm)
                    if existing is None or _better(entry, existing):
                        entries[norm] = entry

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
            self._max_key = max((len(k) for k in entries), default=1)
            self._rules = rules
            self._mtimes = self._snapshot_mtimes()
            self.revision += 1
        if self.corrections is not None:
            # one reload path for the whole vocabulary: a corpus file and a correction
            # arrive through the same call, so there is no way to get one without the other
            self.corrections.load()

    # -- queries ---------------------------------------------------------- #

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def rule_ids(self) -> list[str]:
        return [r.id for r in self._rules]

    def entries_snapshot(self) -> list[Entry]:
        """A stable copy of the loaded entries, for listings and the web panel."""
        with self._lock:
            return sorted(self._entries.values(), key=lambda e: (e.layer, e.source))

    def lookup_exact(self, term: str) -> Entry | None:
        if not term:
            return None
        with self._lock:
            return self._entries.get(normalize(term))

    # -- translation ------------------------------------------------------ #

    def translate(self, text: str, target_lang: str = "zh-CN") -> Outcome:
        """Translate one line using longest-match corpus spans then rules."""
        if not text.strip():
            return Outcome(source_text=text, target_text=text)

        # A corpus edit on disk -- including one this project just made from a user
        # correction -- applies here, at the next line, rather than at the next start.
        self.reload_if_changed()

        # A whole-line correction is checked first and outranks everything: the human
        # saw this exact sentence come out wrong and said what it should say. It is
        # returned as a single span so the rest of the engine sees it as one fully
        # explained, fully confident hit -- which is also what stops the local model
        # from being asked to improve a sentence a person has already settled.
        if self.corrections is not None:
            correction = self.corrections.lookup_line(text)
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
            entries = self._entries
            max_key = self._max_key
            rules = list(self._rules)

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
                translated, used_rule, conf = self._apply_rules(token, entries, rules, target_lang)
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
                entry = entries.get(candidate)
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

        target_text = _join_spans(spans, target_lang)
        return Outcome(source_text=text, target_text=target_text, spans=spans)

    def _apply_rules(
        self,
        token: str,
        entries: dict[str, Entry],
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


def _better(candidate: Entry, incumbent: Entry) -> bool:
    """Pick the winning entry for a duplicate source term."""
    cand_rank = (_LAYER_RANK.get(candidate.layer, 9), -candidate.priority)
    inc_rank = (_LAYER_RANK.get(incumbent.layer, 9), -incumbent.priority)
    return cand_rank < inc_rank


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

    def translate(self, text: str, target_lang: str = "zh-CN") -> Outcome:
        raise NotImplementedError

    def stats(self) -> dict[str, Any]:
        return {}


class CorpusTranslator(Translator):
    """Corpus + rules only. Fully local, no model weights required."""

    name = "corpus+rules"

    def __init__(self, corpus: CorpusStore) -> None:
        self.corpus = corpus
        self._cache: dict[tuple[str, str], Outcome] = {}
        self._hits = 0
        self._misses = 0

    def translate(self, text: str, target_lang: str = "zh-CN") -> Outcome:
        key = (text, target_lang)
        cached = self._cache.get(key)
        if cached is not None:
            self._hits += 1
            return cached
        self._misses += 1
        outcome = self.corpus.translate(text, target_lang)
        if len(self._cache) > 4096:
            self._cache.clear()
        self._cache[key] = outcome
        return outcome

    def stats(self) -> dict[str, Any]:
        total = self._hits + self._misses
        return {
            "backend": self.name,
            "corpus_entries": self.corpus.size,
            "rules": self.corpus.rule_count,
            "rule_ids": self.corpus.rule_ids(),
            "cache_hit_rate": (self._hits / total) if total else 0.0,
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

    def translate(self, text: str, target_lang: str = "zh-CN") -> Outcome:
        if self._llm is None:
            return self.fallback.translate(text, target_lang)
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
            return self.fallback.translate(text, target_lang)
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
