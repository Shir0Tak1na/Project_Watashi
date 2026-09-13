"""Local neural machine translation backend, and the hybrid term-protected path.

Requirement R1 says translation must be local and may use a local model. This
module provides that, with two deliberate design choices:

**1. A purpose-built NMT model, not a chat LLM.**
    CTranslate2 running NLLB-200-distilled-600M (int8) is far faster on CPU than
    a small instruct model and produces grammatical sentences rather than a word
    soup. For subtitles, latency and fluency both matter.

**2. Term protection -- the reason this project exists.**
    A general NMT model gets fiction terminology *wrong*: it renders 剑意 as
    "sword meaning" instead of "sword intent", and invents something different
    every time. So the corpus is authoritative and the model only fills the
    gaps. The pipeline is:

    ::

        source sentence
          -> corpus + rules longest-match scan        (engine, ~0.2 ms)
          -> explained spans replaced by placeholders (@0@, @1@ ...)
          -> NMT translates the remainder
          -> placeholders restored with the corpus translations

    The result is fluent prose *with* consistent terminology, which is exactly
    what a human post-editor needs. Every translation still reports which parts
    came from the corpus and which came from the model.

**Asynchronous by design.**
    NMT on CPU costs hundreds of milliseconds, which does not fit the R4 budget
    for a subtitle frame. ``HybridTranslator`` therefore returns the instant
    corpus/rule result first and refines it in a background worker; the overlay
    updates when the better translation is ready. The corpus path never blocks
    on the model.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .translate import (
    DEFAULT_CONTEXT,
    Context,
    CorpusStore,
    Outcome,
    Span,
    Translator,
    normalize,
)

# --------------------------------------------------------------------------- #
# language codes
# --------------------------------------------------------------------------- #
#
# Language identification and code mapping live in ``lang.py``. They used to be
# duplicated here, and the two copies had already drifted: this one called a
# Japanese line Chinese. Kept as re-exports so existing imports keep working.

from .lang import (  # noqa: E402  (re-exported for backwards compatibility)
    NLLB_CODES,
    UNSPACED as _UNSPACED,
    detect_language,
    to_nllb_code,
)


# --------------------------------------------------------------------------- #
# term protection
# --------------------------------------------------------------------------- #

#: Placeholder scheme. Measured survival rate of terms through NLLB int8,
#: summed over four probe sentences (9 protected terms total):
#:
#:     <=i>=  7/9     <- chosen: the model copies it through verbatim
#:     @i@    4/9     occasionally drops a leading '@' ("@0@" -> "0@")
#:     [i]    4/9
#:     #i#    3/9
#:     [i] CJK brackets, fullwidth @, guillemets   0/9   (dropped or quoted)
#:
#: The angle-bracket form also survived a sentence consisting *only* of
#: placeholders, which the others did not.
DEFAULT_PLACEHOLDER = "\u2264{i}\u2265"

_PLACEHOLDER_PATTERNS = {
    "\u2264{i}\u2265": re.compile(r"\u2264(\d+)\u2265"),
    "@{i}@": re.compile(r"@(\d+)@"),
    "[[{i}]]": re.compile(r"\[\[(\d+)\]\]"),
    "<{i}>": re.compile(r"<(\d+)>"),
    "#{i}#": re.compile(r"#(\d+)#"),
}

#: Below this many residual (non-protected) words there is nothing meaningful
#: left for the model to translate, so the corpus result is returned as final.
#: A line like "antidragon superspirit voidsword" masks to "" and NLLB then
#: emits unrelated text ("其他国家"); skipping it is both faster and correct.
MIN_RESIDUAL_WORDS = 2


def is_degenerate(text: str, max_run: int = 6) -> bool:
    """Detect the repetition loops NLLB occasionally falls into.

    A masked sentence can send the decoder into a loop emitting one token until
    it hits ``max_decoding_length`` (observed: 96 copies of 子). Such output is
    always worse than the corpus result, so it is rejected rather than shown.
    """
    if len(text) < 12:
        return False
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    if max(counts.values()) / len(text) > 0.5:
        return True
    for size in (1, 2, 3):
        unit = text[:size]
        if unit and len(text) > size * max_run and text[: size * max_run] == unit * max_run:
            return True
    return False


#: Anything that counts as real text rather than punctuation, so a masked
#: sentence with only placeholders left is recognised as having no context.
#: Defined here rather than borrowed from a language-detection constant: an
#: earlier refactor removed that constant and this silently became a NameError
#: that only fired on the model path.
_WORD_CHAR = re.compile(
    r"[0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]"
)


def residual_word_count(masked: str, pattern: str = DEFAULT_PLACEHOLDER) -> int:
    """How many real words remain once placeholders are removed."""
    regex = _PLACEHOLDER_PATTERNS.get(pattern)
    residual = regex.sub(" ", masked) if regex else masked
    words = 0
    for piece in residual.split():
        if _WORD_CHAR.search(piece):
            words += 1
    return words


@dataclass
class ProtectedText:
    """A source sentence with corpus terms swapped out for placeholders."""

    masked: str
    terms: list[tuple[str, str]] = field(default_factory=list)  # (placeholder, target)
    pattern: str = DEFAULT_PLACEHOLDER

    @property
    def count(self) -> int:
        return len(self.terms)


def protect_terms(
    text: str,
    outcome: Outcome,
    scheme: str = DEFAULT_PLACEHOLDER,
    min_confidence: float = 0.4,
) -> ProtectedText:
    """Replace every corpus/rule-explained span with a numbered placeholder.

    ``outcome.spans`` is produced by the corpus engine and carries the exact
    source slice plus its authoritative translation, so this is a mechanical
    rewrite rather than any kind of guessing.

    ``min_confidence`` matters more than it looks. The rule set ends with a
    ``transliterate`` fallback that "matches" every unknown word and returns it
    unchanged at confidence 0.10. Treating that as an authoritative term would
    mask the *entire* sentence and leave the model nothing to translate. The
    threshold keeps only real hits (corpus 0.95+, affix/morpheme/template
    0.45+), and it is configurable because rule confidence is declared in the
    rules file by whoever wrote the rule.
    """
    if not outcome.spans:
        return ProtectedText(masked=text, pattern=scheme)

    pieces: list[str] = []
    terms: list[tuple[str, str]] = []
    # spans arrive in order and cover the whole input (literals included)
    for span in outcome.spans:
        if not span.source:
            continue
        authoritative = (
            span.explained
            and span.source.strip()
            and span.confidence >= min_confidence
            and span.target.strip() != span.source.strip()
        )
        if authoritative:
            placeholder = scheme.format(i=len(terms))
            terms.append((placeholder, span.target))
            pieces.append(placeholder)
        else:
            pieces.append(span.source)

    masked = "".join(pieces)
    return ProtectedText(masked=masked, terms=terms, pattern=scheme)


def tidy_cjk_spacing(text: str) -> str:
    """Remove spaces that sit between two CJK characters.

    Masked sources are built from the original spacing, so an English sentence
    like ``the <=0>= of this <=1>= is a myth`` comes back with spaces around the
    restored Chinese terms: ``对于此 宗门的 剑意是个神话``. Those spaces are
    artefacts of the source layout, not real punctuation, and a reader should
    not see them.
    """
    cjk = r"\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u3000-\u303f\uff00-\uffef"
    return re.sub(rf"(?<=[{cjk}])[ \t]+(?=[{cjk}])", "", text)


def restore_terms(text: str, protected: ProtectedText) -> tuple[str, list[str]]:
    """Put the corpus translations back. Returns the text and any lost terms."""
    pattern = _PLACEHOLDER_PATTERNS.get(protected.pattern)
    if pattern is None:
        return text, [placeholder for placeholder, _ in protected.terms]

    mapping = dict(protected.terms)

    def swap(match: re.Match[str]) -> str:
        return mapping.get(match.group(0), match.group(0))

    restored = pattern.sub(swap, text)
    lost = [placeholder for placeholder, _ in protected.terms if placeholder not in text]
    return restored, lost

# --------------------------------------------------------------------------- #
# the model
# --------------------------------------------------------------------------- #


@dataclass
class NmtStats:
    calls: int = 0
    sentences: int = 0
    total_ms: float = 0.0
    protected_terms: int = 0
    lost_placeholders: int = 0
    errors: int = 0

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0


class NmtModel:
    """CTranslate2 seq2seq model plus its fast tokenizer.

    Loading is explicit (``load``) so the caller can show progress and so a
    failure to load degrades to the corpus engine instead of crashing the app.
    """

    def __init__(
        self,
        model_dir: str | Path,
        compute_type: str = "int8",
        beam_size: int = 1,
        intra_threads: int = 4,
        inter_threads: int = 1,
        max_decoding_length: int = 192,
        max_input_length: int = 256,
        placeholder_scheme: str = DEFAULT_PLACEHOLDER,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.intra_threads = intra_threads
        self.inter_threads = inter_threads
        self.max_decoding_length = max_decoding_length
        self.max_input_length = max_input_length
        self.placeholder_scheme = placeholder_scheme

        self._translator: Any = None
        self._tokenizer: Any = None
        self._lock = threading.Lock()
        self.load_ms: float = 0.0
        self.load_error: str | None = None
        self.stats = NmtStats()
        self.applied: dict[str, Any] = {}

    # -- lifecycle -------------------------------------------------------- #

    @property
    def loaded(self) -> bool:
        return self._translator is not None

    @property
    def available(self) -> bool:
        return self.loaded

    def missing_files(self) -> list[str]:
        required = ("model.bin", "config.json", "tokenizer.json")
        return [name for name in required if not (self.model_dir / name).exists()]

    def load(self) -> bool:
        """Load the model. Returns False (with ``load_error`` set) on failure."""
        if self._translator is not None:
            return True
        with self._lock:
            if self._translator is not None:
                return True

            missing = self.missing_files()
            if missing:
                self.load_error = (
                    f"model incomplete at {self.model_dir} (missing {', '.join(missing)}); "
                    f"run: prototype\run.cmd fetch_model"
                )
                return False

            started = time.perf_counter()
            try:
                import ctranslate2
                from tokenizers import Tokenizer

                self._translator = ctranslate2.Translator(
                    str(self.model_dir),
                    device="cpu",
                    compute_type=self.compute_type,
                    intra_threads=self.intra_threads,
                    inter_threads=self.inter_threads,
                )
                self._tokenizer = Tokenizer.from_file(
                    str(self.model_dir / "tokenizer.json")
                )
                self._trim_tokenizer()
            except Exception as exc:
                self.load_error = f"{type(exc).__name__}: {exc}"
                self._translator = None
                return False

            self.applied = {
                "compute_type": self.compute_type,
                "beam_size": self.beam_size,
                "intra_threads": self.intra_threads,
                "inter_threads": self.inter_threads,
            }
            self.load_ms = (time.perf_counter() - started) * 1000.0
            return True

    def _trim_tokenizer(self) -> None:
        """Encode without padding, and truncate long inputs rather than fail."""
        tokenizer = self._tokenizer
        try:
            tokenizer.no_padding()
        except Exception:
            pass
        try:
            tokenizer.enable_truncation(max_length=self.max_input_length)
        except Exception:
            pass

    # -- tokenisation ----------------------------------------------------- #

    def encode(self, text: str, source_lang: str) -> list[str]:
        """Build the source token sequence NLLB expects.

        Two details are load bearing and were found by measurement:

        * the sequence must **end with </s>**. Without it the decoder never
          emits an end token, runs to ``max_decoding_length`` and returns a
          repetition loop -- and takes ~20x longer doing it
          (7100 ms instead of 355 ms for one sentence).
        * the sequence must **start with the source language token**.

        ``add_special_tokens=True`` is not usable here: this fast tokenizer
        appends both ``</s>`` and a spurious ``<unk>``.
        """
        tokens = list(self._tokenizer.encode(text, add_special_tokens=False).tokens)
        if not tokens or tokens[0] != source_lang:
            tokens = [source_lang] + tokens
        tokens = tokens[: self.max_input_length]
        tokens.append("</s>")
        return tokens

    def decode(self, tokens: Sequence[str], target_lang: str) -> str:
        """Turn output tokens back into text.

        The ``tokenizers`` library is not the Hugging Face ``PreTrainedTokenizer``
        API: there is no ``convert_tokens_to_ids``, so ids are looked up one
        token at a time.
        """
        cleaned = [t for t in tokens if t not in (target_lang, "<s>", "</s>", "<pad>")]
        ids = []
        for token in cleaned:
            token_id = self._tokenizer.token_to_id(token)
            if token_id is not None:
                ids.append(token_id)
        if not ids:
            return ""
        text = self._tokenizer.decode(ids, skip_special_tokens=True)
        return text.strip()

    # -- inference -------------------------------------------------------- #

    def translate_batch(
        self,
        texts: Sequence[str],
        target_lang: str,
        source_lang: str | None = None,
    ) -> list[str]:
        """Translate several sentences at once (batching is much faster)."""
        if not self.loaded:
            raise RuntimeError(self.load_error or "model not loaded")
        if not texts:
            return []

        sources: list[list[str]] = []
        for text in texts:
            src = source_lang or detect_language(text)
            sources.append(self.encode(text, src))

        started = time.perf_counter()
        try:
            results = self._translator.translate_batch(
                sources,
                target_prefix=[[target_lang]] * len(sources),
                beam_size=self.beam_size,
                max_decoding_length=self.max_decoding_length,
                return_scores=False,
            )
        except Exception:
            self.stats.errors += 1
            raise
        elapsed = (time.perf_counter() - started) * 1000.0

        outputs: list[str] = []
        for result in results:
            hypothesis = result.hypotheses[0] if result.hypotheses else []
            outputs.append(self.decode(hypothesis, target_lang))

        self.stats.calls += 1
        self.stats.sentences += len(texts)
        self.stats.total_ms += elapsed
        return outputs

    def translate_one(
        self, text: str, target_lang: str, source_lang: str | None = None
    ) -> str:
        return self.translate_batch([text], target_lang, source_lang)[0]


# --------------------------------------------------------------------------- #
# translators
# --------------------------------------------------------------------------- #


@dataclass
class Refinement:
    """The outcome of running the NMT model over one line."""

    source_text: str
    target_text: str
    backend: str
    protected_terms: int = 0
    lost_placeholders: int = 0
    elapsed_ms: float = 0.0
    used_nmt: bool = True
    #: why the model result was rejected, when it was
    fallback_reason: str | None = None

    @property
    def is_model_output(self) -> bool:
        return self.used_nmt and self.backend.startswith("local-nmt")


def refine_with_nmt(
    model: NmtModel,
    corpus: CorpusStore,
    text: str,
    target_lang: str,
    protect: bool = True,
    min_confidence: float = 0.4,
    source_lang: str | None = None,
) -> Refinement:
    """Translate one line with the model, keeping corpus terminology authoritative.

    The decision sequence, each step of which was chosen from measurement:

    1. **Term-density skip.** If masking the corpus terms leaves almost nothing
       to translate, the model has no context and emits unrelated text, so the
       corpus result is returned instead. ``antidragon superspirit voidsword``
       masks to an empty string; NLLB answers "其他国家".
    2. **Mask, translate, restore.** Corpus terms become ``<=0>=`` style
       placeholders and are substituted back afterwards.
    3. **Placeholder verification.** If any placeholder failed to survive, the
       terms cannot be trusted, so the corpus result is returned. Terminology
       correctness outranks fluency for this project.
    4. **Degeneracy guard.** Reject repetition loops (the model occasionally
       emits one token until it hits the decoding cap).
    """
    corpus_outcome = corpus.translate(text, target_lang)

    def fallback(
        reason: str, elapsed: float = 0.0, terms: int = 0, lost: int = 0
    ) -> Refinement:
        return Refinement(
            source_text=text,
            target_text=corpus_outcome.target_text,
            backend="corpus+rules",
            protected_terms=terms,
            lost_placeholders=lost,
            elapsed_ms=elapsed,
            used_nmt=False,
            fallback_reason=reason,
        )

    if not text.strip():
        return fallback("empty input")
    if not model.available:
        return fallback(model.load_error or "model unavailable")
    try:
        nllb_target = to_nllb_code(target_lang)
    except ValueError:
        return fallback(f"no NLLB code for target {target_lang!r}")

    if protect:
        protected = protect_terms(
            text, corpus_outcome, model.placeholder_scheme, min_confidence
        )
    else:
        protected = ProtectedText(masked=text, pattern=model.placeholder_scheme)

    if protected.count and residual_word_count(protected.masked, protected.pattern) < MIN_RESIDUAL_WORDS:
        return fallback("no context left after masking terms", terms=protected.count)

    resolved_source = source_lang
    if resolved_source:
        try:
            resolved_source = to_nllb_code(resolved_source)
        except ValueError:
            resolved_source = None

    started = time.perf_counter()
    try:
        raw = model.translate_one(protected.masked, nllb_target, resolved_source)
    except Exception as exc:
        return fallback(f"model error: {exc}", (time.perf_counter() - started) * 1000.0)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    if not raw.strip():
        return fallback("model returned nothing", elapsed_ms, protected.count)
    if is_degenerate(raw):
        return fallback("model output was a repetition loop", elapsed_ms, protected.count)

    if not protected.count:
        return Refinement(
            source_text=text,
            target_text=raw,
            backend=f"local-nmt/{model.compute_type}",
            elapsed_ms=elapsed_ms,
        )

    restored, lost = restore_terms(raw, protected)
    if nllb_target.split("_")[0] in _UNSPACED:
        restored = tidy_cjk_spacing(restored)
    model.stats.protected_terms += protected.count
    model.stats.lost_placeholders += len(lost)
    if lost:
        return fallback(
            f"{len(lost)}/{protected.count} corpus terms lost through the model",
            elapsed_ms,
            protected.count,
            len(lost),
        )

    return Refinement(
        source_text=text,
        target_text=restored,
        backend=f"local-nmt/{model.compute_type}",
        protected_terms=protected.count,
        lost_placeholders=0,
        elapsed_ms=elapsed_ms,
    )


def refine_batch_with_nmt(
    model: NmtModel,
    corpus: CorpusStore,
    texts: Sequence[str],
    target_lang: str,
    protect: bool = True,
    min_confidence: float = 0.4,
    source_lang: str | None = None,
) -> list[Refinement]:
    """Refine several lines with **one** batched model call.

    Batching is the single biggest win available here: measured at 2.8-3.5x
    faster per sentence than one call each, which is what makes a multi-line
    frame affordable at all.

    The decision logic is per line -- term density, placeholders, degeneracy --
    exactly as in :func:`refine_with_nmt`; only the inference is shared.
    """
    if not texts:
        return []

    prepared: list[tuple[str, Outcome, "ProtectedText"]] = []
    results: dict[int, Refinement] = {}

    def fallback_for(index: int, text: str, outcome: Outcome, reason: str,
                     terms: int = 0, lost: int = 0) -> None:
        results[index] = Refinement(
            source_text=text,
            target_text=outcome.target_text,
            backend="corpus+rules",
            protected_terms=terms,
            lost_placeholders=lost,
            used_nmt=False,
            fallback_reason=reason,
        )

    nllb_target: str | None
    try:
        nllb_target = to_nllb_code(target_lang)
    except ValueError as exc:
        nllb_target = None
        reason = f"no NLLB code for target {target_lang!r}: {exc}"

    for index, text in enumerate(texts):
        outcome = corpus.translate(text, target_lang)
        if nllb_target is None:
            fallback_for(index, text, outcome, reason)
            continue
        if not text.strip():
            fallback_for(index, text, outcome, "empty input")
            continue
        if not model.available:
            fallback_for(index, text, outcome, model.load_error or "model unavailable")
            continue

        protected = (
            protect_terms(text, outcome, model.placeholder_scheme, min_confidence)
            if protect
            else ProtectedText(masked=text, pattern=model.placeholder_scheme)
        )
        if (
            protected.count
            and residual_word_count(protected.masked, protected.pattern) < MIN_RESIDUAL_WORDS
        ):
            fallback_for(index, text, outcome, "no context left after masking terms",
                         terms=protected.count)
            continue
        prepared.append((text, outcome, protected))

    if prepared:
        resolved_source = source_lang
        if resolved_source:
            try:
                resolved_source = to_nllb_code(resolved_source)
            except ValueError:
                resolved_source = None

        masked = [item[2].masked for item in prepared]
        batch_started = time.perf_counter()
        try:
            raws = model.translate_batch(masked, nllb_target or "", resolved_source)
            batch_error: str | None = None
        except Exception as exc:
            raws = []
            batch_error = str(exc)
        batch_ms = (time.perf_counter() - batch_started) * 1000.0
        # One call covers every prepared line, so per-line latency is the batch
        # cost divided by the work it did. Reporting 0 here would have been a
        # silent lie in the stats.
        per_line_ms = batch_ms / max(1, len(masked))

        if batch_error is not None:
            for text, outcome, protected in prepared:
                fallback_for(
                    index=texts.index(text), text=text, outcome=outcome,
                    reason=f"model error: {batch_error}", terms=protected.count,
                )
            return [results[i] for i in sorted(results)]

        for (text, outcome, protected), raw in zip(prepared, raws):
            index = texts.index(text)
            if not raw.strip():
                fallback_for(index, text, outcome, "model returned nothing",
                             terms=protected.count)
                continue
            if is_degenerate(raw):
                fallback_for(index, text, outcome, "model output was a repetition loop",
                             terms=protected.count)
                continue
            if not protected.count:
                results[index] = Refinement(
                    source_text=text,
                    target_text=raw,
                    backend=f"local-nmt/{model.compute_type}",
                    elapsed_ms=per_line_ms,
                )
                continue

            restored, lost = restore_terms(raw, protected)
            if nllb_target and nllb_target.split("_")[0] in _UNSPACED:
                restored = tidy_cjk_spacing(restored)
            model.stats.protected_terms += protected.count
            model.stats.lost_placeholders += len(lost)
            if lost:
                fallback_for(
                    index, text, outcome,
                    f"{len(lost)}/{protected.count} corpus terms lost through the model",
                    terms=protected.count, lost=len(lost),
                )
                continue
            results[index] = Refinement(
                source_text=text,
                target_text=restored,
                backend=f"local-nmt/{model.compute_type}",
                protected_terms=protected.count,
                elapsed_ms=per_line_ms,
            )

    return [results[i] for i in sorted(results)]


class NmtTranslator(Translator):
    """Synchronous NMT translator with corpus term protection."""

    name = "local-nmt"

    def __init__(
        self,
        model: NmtModel,
        corpus: CorpusStore,
        protect_terms: bool = True,
        source_lang: str | None = None,
        protect_min_confidence: float = 0.4,
    ) -> None:
        self.model = model
        self.corpus = corpus
        self.protect = protect_terms
        self.protect_min_confidence = protect_min_confidence
        self.source_lang = None if (source_lang or "auto") == "auto" else source_lang

    def translate(self, text: str, target_lang: str = "zh-CN") -> Outcome:
        refinement = refine_with_nmt(
            self.model,
            self.corpus,
            text,
            target_lang,
            self.protect,
            self.protect_min_confidence,
            self.source_lang,
        )
        return Outcome(
            source_text=text,
            target_text=refinement.target_text,
            spans=[
                Span(
                    source=text,
                    target=refinement.target_text,
                    origin="nmt" if refinement.used_nmt else "corpus",
                    confidence=0.75 if refinement.used_nmt else 0.9,
                    rule_id=refinement.backend
                    + (f" ({refinement.fallback_reason})" if refinement.fallback_reason else ""),
                )
            ],
            backend=refinement.backend,
        )

    def stats(self) -> dict[str, Any]:
        base = {
            "backend": self.name if self.model.available else "corpus+rules",
            "corpus_entries": self.corpus.size,
            "rules": self.corpus.rule_count,
            "rule_ids": self.corpus.rule_ids(),
            "nmt_available": self.model.available,
            "nmt_error": self.model.load_error,
            "nmt_load_ms": round(self.model.load_ms, 1),
            "nmt_mean_ms": round(self.model.stats.mean_ms, 1),
            "nmt_sentences": self.model.stats.sentences,
            "nmt_errors": self.model.stats.errors,
            "nmt_protected_terms": self.model.stats.protected_terms,
            "nmt_lost_placeholders": self.model.stats.lost_placeholders,
        }
        return base


class HybridTranslator(Translator):
    """Fast corpus result now, NMT refinement asynchronously.

    This is what makes an NMT model usable under the R4 latency budget: the
    subtitle bar is never waiting on the model. ``submit`` queues work, the
    worker refines it in the background and hands the improved text to
    ``on_refined``.

    Refinement is **batched per line and capped**, which matters once the capture
    is a whole window rather than a subtitle strip. Measured: a full-window frame
    produced ~25 lines, the model took 4.7 s on the joined text, and OCR itself
    rose to 2.9 s per frame. Batching is 2.8-3.5x faster than one call per
    sentence, and the cap stops a page of text from being treated as a subtitle.
    """

    name = "hybrid(corpus+nmt)"

    #: Refining more lines than this per frame is not worth the latency: a
    #: subtitle frame has a handful of lines, a page has dozens.
    DEFAULT_MAX_LINES = 6
    #: Total characters of work per frame.
    DEFAULT_MAX_CHARS = 600

    def __init__(
        self,
        corpus: CorpusStore,
        model: NmtModel | None = None,
        protect_terms: bool = True,
        on_refined: Callable[[Refinement], None] | None = None,
        source_lang: str | None = None,
        cache_size: int = 512,
        protect_min_confidence: float = 0.4,
        max_lines_per_refinement: int = DEFAULT_MAX_LINES,
        max_chars_per_refinement: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self.corpus = corpus
        self.model = model
        self.protect = protect_terms
        self.on_refined = on_refined
        self.source_lang = None if (source_lang or "auto") == "auto" else source_lang
        self.cache_size = cache_size
        self.protect_min_confidence = protect_min_confidence
        self.max_lines_per_refinement = max_lines_per_refinement
        self.max_chars_per_refinement = max_chars_per_refinement
        self.skipped_too_long = 0
        self._cache_revision = self._revision()

        self._latest: tuple[list[str], str, int] | None = None
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cache: dict[tuple[str, str], Refinement] = {}
        self._cache_revision = 0
        self._lock = threading.Lock()
        self.refinements = 0
        self.dropped = 0
        self.deferred = 0
        #: refinements thrown away because the corpus changed while the model was
        #: working on them. Counted rather than silent: a correction that keeps being
        #: discarded looks exactly like a correction that does not work.
        self.stale_dropped = 0
        #: Optional predicate: while it returns True the worker holds off.
        #: Set by the pipeline so the model does not steal memory bandwidth
        #: from OCR. Bounded by ``max_defer_seconds`` so a constantly changing
        #: screen still gets refinements.
        self.defer_while: Callable[[], bool] | None = None
        self.max_defer_seconds: float = 4.0

    # -- fast path -------------------------------------------------------- #

    @property
    def model_available(self) -> bool:
        return self.model is not None and self.model.available

    def translate(
        self,
        text: str,
        target_lang: str = "zh-CN",
        context: "Context | None" = None,
    ) -> Outcome:
        """Instant result; never touches the model."""
        outcome = self.corpus.translate(text, target_lang, context)
        cached = self._cached(text, target_lang, context)
        if cached is not None:
            outcome.target_text = cached.target_text
            outcome.backend = cached.backend
        return outcome

    def _cache_key(
        self, text: str, target_lang: str, context: "Context | None"
    ) -> tuple[str, ...]:
        """``(text, *what the corpus can distinguish)``, target language included.

        A cached refinement is an answer about a specific situation. Keying it on the
        text alone served a Chinese refinement to a request for Japanese, and made two
        senses of one term share one answer -- the corpus would pick the right entry and
        the cache would then paint the old translation over it.

        The context is normalised against the argument first: a caller that passes
        ``Context()`` and a target language separately would otherwise key on the empty
        string, and every language would share one cache.
        """
        resolved = (context or DEFAULT_CONTEXT).with_target(target_lang)
        return (text, *self.corpus.context_key(resolved))

    def _cached(
        self, text: str, target_lang: str, context: "Context | None" = None
    ) -> Refinement | None:
        """The cached refinement for this text, if it is still valid.

        A refined answer describes the vocabulary it was computed against. Once the
        corpus changes -- a correction, a reload, or an edit the mtime check noticed --
        every cached answer is an answer to a question that has been re-answered, so
        the cache is emptied rather than checked entry by entry. Serving one would
        mean a hand edit to a corpus file appearing not to work, because the model's
        older answer for the same line was still being handed back.
        """
        with self._lock:
            if self._cache_revision != self._revision():
                self._cache.clear()
                self._cache_revision = self._revision()
                return None
            return self._cache.get(self._cache_key(text, target_lang, context))

    def submit(
        self,
        texts: "str | Sequence[str]",
        target_lang: str,
        context: "Context | None" = None,
    ) -> bool:
        """Queue refinement. Accepts one string or a list of lines.

        Only the newest request is kept: a screen that keeps changing should not
        build a backlog of work describing frames that are already gone.

        The list is trimmed to the configured caps. A whole-window capture can
        produce two dozen lines, and refining all of them is both slow and beside
        the point -- a subtitle frame has a handful of lines.
        """
        if not self.model_available:
            return False
        if isinstance(texts, str):
            candidates = [texts]
        else:
            candidates = [t for t in texts if t and t.strip()]
        if not candidates:
            return False

        selected: list[str] = []
        budget = self.max_chars_per_refinement
        for text in candidates:
            if len(selected) >= self.max_lines_per_refinement:
                self.skipped_too_long += 1
                break
            if selected and len(text) > budget:
                self.skipped_too_long += 1
                break
            selected.append(text)
            budget -= len(text)
        if not selected:
            return False
        if not any(
            self._cached(text, target_lang, context) is None for text in selected
        ):
            return False

        with self._condition:
            if self._latest is not None:
                self.dropped += 1
            # The vocabulary revision travels with the job: reading the corpus now and
            # comparing later is the only way to notice that a human corrected one of
            # these lines while the model was still thinking about it. The context key
            # travels with it too, so the answer is cached under the situation it was
            # computed for rather than under the text alone.
            self._latest = (
                selected,
                target_lang,
                self._revision(),
                self.corpus.context_key(
                    (context or DEFAULT_CONTEXT).with_target(target_lang)
                ),
            )
            self._condition.notify()
        return True

    def _revision(self) -> int:
        return int(getattr(self.corpus, "revision", 0))

    def context_key(self, context: "Context | None") -> tuple[str, ...]:
        """What the corpus can distinguish, so the pipeline's memories can key on it.

        Delegated rather than reimplemented: the pipeline's recent-translation memory
        and this class's refinement cache have to agree with the corpus about what a
        different situation is, or one of them will serve an answer the other would
        have refused.
        """
        return tuple(self.corpus.context_key(context))

    def forget(self, source: str | None = None, contains: str | None = None) -> int:
        """Drop cached refinements the vocabulary has moved past.

        A refined answer is cached against the exact source text and reused for the
        next frame that reads it. After a correction that cached answer is the one the
        user rejected, so it has to go -- otherwise the correction would be applied by
        the corpus and then immediately overwritten by the cache on the very next
        frame, which is indistinguishable from the correction not working.
        """
        with self._lock:
            doomed = [
                key
                for key in self._cache
                if (source is not None and key[0] == source)
                or (contains is not None and contains in key[0])
            ]
            for key in doomed:
                del self._cache[key]
        return len(doomed)

    # -- worker ----------------------------------------------------------- #

    def start(self) -> None:
        if self._thread is not None or not self.model_available:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._work_loop, name="watashi-nmt", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _work_loop(self) -> None:
        while not self._stop.is_set():
            with self._condition:
                while self._latest is None and not self._stop.is_set():
                    self._condition.wait(timeout=0.2)
                job = self._latest
                self._latest = None
            if job is None:
                continue
            texts, target_lang, revision, context_key = job
            self._wait_for_quiet_period()
            if self._stop.is_set():
                return
            try:
                refinements = self._refine_batch(texts, target_lang)
            except Exception as exc:
                print(f"[nmt] refinement failed: {exc}")
                continue
            if not refinements:
                continue
            if revision != self._revision():
                # The vocabulary was replaced while this batch was being computed -- a
                # correction, a reload, or an edit noticed by the mtime check. Discarding
                # it is the point: publishing would put the pre-correction answer back on
                # screen, and caching it would keep serving it to later frames.
                self.stale_dropped += len(refinements)
                continue
            with self._lock:
                if len(self._cache) >= self.cache_size:
                    self._cache.clear()
                for refinement in refinements:
                    # The job's context key, not the text's: computed under the situation
                    # the batch was queued for, so a scene change mid-flight cannot file
                    # the answer under the new one. Same shape ``_cache_key`` builds --
                    # text first -- because ``forget`` reads ``key[0]`` as the source.
                    self._cache[(refinement.source_text, *context_key)] = refinement
            self.refinements += len(refinements)
            if self.on_refined is not None:
                for refinement in refinements:
                    self.on_refined(refinement)

    def _wait_for_quiet_period(self) -> None:
        """Hold off while OCR is busy, but never indefinitely.

        NLLB inference and OCR convolution both saturate memory bandwidth, so
        running them together costs far more than running them in sequence:
        OCR went from 73 ms to 266 ms in measurement. Subtitles sit still for
        seconds at a time, so waiting for the gap is nearly free.

        The wait is bounded: if the screen keeps changing, refinement proceeds
        anyway rather than never happening.
        """
        if self.defer_while is None:
            return
        deadline = time.perf_counter() + self.max_defer_seconds
        waited = False
        while time.perf_counter() < deadline:
            if self._stop.is_set():
                return
            try:
                busy = bool(self.defer_while())
            except Exception:
                return
            if not busy:
                break
            waited = True
            time.sleep(0.01)
        if waited:
            self.deferred += 1

    def _refine_batch(self, texts: Sequence[str], target_lang: str) -> list[Refinement]:
        assert self.model is not None
        return refine_batch_with_nmt(
            self.model,
            self.corpus,
            texts,
            target_lang,
            self.protect,
            self.protect_min_confidence,
            self.source_lang,
        )

    # -- reporting -------------------------------------------------------- #

    def stats(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "backend": self.name if self.model_available else "corpus+rules",
            "corpus_entries": self.corpus.size,
            "corpus_languages": self.corpus.language_summary(),
            "rules": self.corpus.rule_count,
            "rule_ids": self.corpus.rule_ids(),
            "nmt_available": self.model_available,
            "refinements": self.refinements,
            "refinements_dropped": self.dropped,
            "refinements_deferred": self.deferred,
            "refinements_stale": self.stale_dropped,
            #: whether a job is queued right now -- distinct from "dropped",
            #: which counts requests superseded before they ran
            "refinements_pending": 1 if self._latest is not None else 0,
            # The corrections counts, published from *here* because this is the class the
            # application actually builds: adding them to ``CorpusTranslator.stats()``
            # alone would have left the real surfaces reading a dict that never carried
            # them, which a self check caught immediately and a user never would have.
            **self.corpus.correction_stats(),
        }
        if self.model is not None:
            data.update(
                {
                    "nmt_error": self.model.load_error,
                    "nmt_load_ms": round(self.model.load_ms, 1),
                    "nmt_mean_ms": round(self.model.stats.mean_ms, 1),
                    "nmt_sentences": self.model.stats.sentences,
                    "nmt_errors": self.model.stats.errors,
                    "nmt_protected_terms": self.model.stats.protected_terms,
                    "nmt_lost_placeholders": self.model.stats.lost_placeholders,
                }
            )
        return data


# --------------------------------------------------------------------------- #
# convenience
# --------------------------------------------------------------------------- #


def load_hybrid_translator(
    corpus: CorpusStore,
    model_dir: str | Path | None,
    on_refined: Callable[[Refinement], None] | None = None,
    protect_terms: bool = True,
    source_lang: str | None = None,
    compute_type: str = "int8",
    beam_size: int = 1,
    intra_threads: int = 4,
    inter_threads: int = 1,
    placeholder_scheme: str = DEFAULT_PLACEHOLDER,
    protect_min_confidence: float = 0.4,
) -> HybridTranslator:
    """Build the hybrid translator, loading the model only when configured."""
    model: NmtModel | None = None
    if model_dir:
        model = NmtModel(
            model_dir,
            compute_type=compute_type,
            beam_size=beam_size,
            intra_threads=intra_threads,
            inter_threads=inter_threads,
            placeholder_scheme=placeholder_scheme,
        )
        model.load()
        if not model.available:
            print(f"[nmt] model unavailable: {model.load_error}")
    return HybridTranslator(
        corpus=corpus,
        model=model,
        protect_terms=protect_terms,
        on_refined=on_refined,
        source_lang=source_lang,
        protect_min_confidence=protect_min_confidence,
    )
