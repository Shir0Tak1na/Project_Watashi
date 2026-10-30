// Local NMT backend: corpus/rules first, NLLB-200 (CTranslate2 int8) for the
// rest, with the terminology kept authoritative.
//
// This mirrors ``prototype/watashi/local_nmt.py`` behaviour-for-behaviour for the
// "readable" path, which is the one the product uses:
//
//   1. the corpus/rules engine answers first;
//   2. a corpus answer that is already complete is final (zero model cost);
//   3. otherwise the *code-switched* corpus sentence (source grammar with target
//      terms inline) is translated as a whole, because masking terms leaves the
//      model too little grammar to work with;
//   4. the corpus terms must survive. If one was lost, the line is retried once
//      with ``<=i>`` placeholders; if that loses them too, the fluent model
//      sentence is kept and the loss is reported -- never the half-English
//      corpus string.
//
// Every result carries the facts the Python code reports: backend string,
// protected_terms, lost_placeholders, used_model and fallback_reason. The
// strings are copied literally from ``local_nmt.py`` so a differential test can
// require exact equality instead of "close enough".
//
// The model is injected through ``NmtEngine`` so the decision logic can be
// tested without a model, a tokenizer or three seconds of loading.

#pragma once

#include <cstddef>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include "watashi/corpus.hpp"
#include "watashi/translator.hpp"

namespace watashi {

/// Mirrors ``local_nmt.NmtStats``.
struct NmtStats {
    int calls = 0;
    int sentences = 0;
    double total_ms = 0.0;
    int protected_terms = 0;
    int lost_placeholders = 0;
    int errors = 0;

    [[nodiscard]] double mean_ms() const {
        return calls > 0 ? total_ms / static_cast<double>(calls) : 0.0;
    }
};

/// Where the model lives and how it runs. Defaults match the Python model.
struct NmtOptions {
    std::string model_dir;
    std::string compute_type = "int8";
    int beam_size = 1;
    int intra_threads = 4;
    int max_decoding_length = 192;
    int max_input_length = 256;
};

/// The model seam. ``LocalNmt`` is the real one; tests implement a scripted one.
class NmtEngine {
public:
    virtual ~NmtEngine() = default;

    [[nodiscard]] virtual bool available() const = 0;
    [[nodiscard]] virtual std::string load_error() const = 0;
    [[nodiscard]] virtual std::string compute_type() const = 0;
    [[nodiscard]] virtual double load_ms() const = 0;

    /// Translate decoded sentences; returns decoded sentences in the same order.
    /// Throws std::runtime_error on a model failure.
    [[nodiscard]] virtual std::vector<std::string> translate_batch(
        const std::vector<std::string>& texts,
        const std::string& target_nllb,
        const std::string& source_nllb) = 0;

    [[nodiscard]] virtual const NmtStats& stats() const = 0;
};

/// CTranslate2 + SentencePiece. Loading happens in the constructor: a bad path
/// sets ``load_error`` instead of throwing, exactly like ``NmtModel.load``.
class LocalNmt final : public NmtEngine {
public:
    explicit LocalNmt(NmtOptions options);
    ~LocalNmt() override;

    LocalNmt(const LocalNmt&) = delete;
    LocalNmt& operator=(const LocalNmt&) = delete;
    LocalNmt(LocalNmt&&) noexcept;
    LocalNmt& operator=(LocalNmt&&) noexcept;

    [[nodiscard]] bool available() const override;
    [[nodiscard]] std::string load_error() const override;
    [[nodiscard]] std::string compute_type() const override;
    [[nodiscard]] double load_ms() const override;
    [[nodiscard]] std::vector<std::string> translate_batch(
        const std::vector<std::string>& texts,
        const std::string& target_nllb,
        const std::string& source_nllb) override;
    [[nodiscard]] const NmtStats& stats() const override;

    /// The source token sequence Python feeds CTranslate2: source language
    /// token, SentencePiece pieces, then ``</s>``. Exposed for tests so the
    /// tokenizer can be compared with the Python reference without a model.
    [[nodiscard]] static std::vector<std::string> encode_tokens(
        const std::vector<std::string>& pieces,
        const std::string& source_lang,
        int max_input_length);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

/// Behaviour copied from ``local_nmt.py``. Everything here is pure: it has no
/// model, no I/O and no clock, which is what makes the no-model tests possible.
namespace nmt_detail {

/// ``lang.to_nllb_code``. Throws std::invalid_argument for an unknown tag.
[[nodiscard]] std::string to_nllb_code(std::string_view tag);

/// ``lang.detect_language``; ``fallback`` is returned when no script decides.
[[nodiscard]] std::string detect_language(std::string_view text,
                                          std::string_view fallback = "eng_Latn");

/// ``lang.decisive_language``; empty when the verdict would rest on Latin.
[[nodiscard]] std::string decisive_language(std::string_view text);

/// ``lang.has_translatable_content``: at least one Unicode letter.
[[nodiscard]] bool has_translatable_content(std::string_view text);

/// ``lang.matches_target``.
[[nodiscard]] bool matches_target(std::string_view text, std::string_view target_lang);

/// ``local_nmt.is_degenerate``.
[[nodiscard]] bool is_degenerate(std::string_view text, int max_run = 6);

/// ``local_nmt.tidy_cjk_spacing``.
[[nodiscard]] std::string tidy_cjk_spacing(std::string_view text);

/// Code points, matching Python's ``len`` on a ``str``.
[[nodiscard]] std::size_t codepoint_count(std::string_view text);

/// True when there is nothing but whitespace (Python's ``not text.strip()``).
[[nodiscard]] bool is_blank(std::string_view text);

/// Placeholder scheme and the masked sentence.
struct ProtectedText {
    std::string masked;
    std::vector<std::pair<std::string, std::string>> terms;  // (placeholder, target)
    std::string pattern = "\u2264{i}\u2265";

    [[nodiscard]] int count() const { return static_cast<int>(terms.size()); }
};

[[nodiscard]] ProtectedText protect_terms(std::string_view text,
                                          const Outcome& outcome,
                                          std::string_view scheme = "\u2264{i}\u2265",
                                          double min_confidence = 0.4);

/// Returns the restored text and the placeholders that did not survive.
[[nodiscard]] std::pair<std::string, std::vector<std::string>> restore_terms(
    std::string_view text, const ProtectedText& protected_text);

[[nodiscard]] int residual_word_count(std::string_view masked,
                                      std::string_view scheme = "\u2264{i}\u2265");

/// Corpus translations that must survive this line, in span order.
[[nodiscard]] std::vector<std::string> term_targets(const Outcome& outcome,
                                                    double min_confidence = 0.4);

/// Terms absent from ``text``, ignoring whitespace.
[[nodiscard]] std::vector<std::string> missing_terms(std::string_view text,
                                                     const std::vector<std::string>& targets);

/// ``_corpus_is_complete``: the corpus answer already needs no model.
[[nodiscard]] bool corpus_is_complete(const Outcome& outcome, std::string_view target_lang);

/// ``_usable_model_text``: real text, no loop, in the target language.
[[nodiscard]] bool usable_model_text(std::string_view text, std::string_view target_lang);

/// One source-language bucket. Indices refer to the input vector.
struct SourceGroup {
    std::string code;
    std::vector<std::size_t> indices;
};

/// Group lines by source language, preserving first-seen order. ``source_lang``
/// empty or "auto" means detect per line.
[[nodiscard]] std::vector<SourceGroup> group_by_source(
    const std::vector<std::string>& texts,
    std::string_view source_lang);

}  // namespace nmt_detail

/// Options for the corpus-first NMT translator.
struct NmtTranslatorOptions {
    bool protect = true;
    double protect_min_confidence = 0.4;
    std::string source_lang;  // empty or "auto" = detect
    std::string placeholder_scheme = "\u2264{i}\u2265";
};

/// The ``Translator`` implementation the CLI and the pipeline use.
class NmtTranslator final : public Translator {
public:
    NmtTranslator(CorpusStore& corpus, NmtEngine& engine, NmtTranslatorOptions options = {});

    [[nodiscard]] Translation translate(
        std::string_view source, const TranslateContext& context) const override;

    [[nodiscard]] std::vector<Translation> translate_batch(
        const std::vector<std::string>& sources,
        const TranslateContext& context) const override;

    [[nodiscard]] const NmtEngine& engine() const { return *engine_; }
    /// Engine counters (calls/sentences/time/errors) merged with the term
    /// counters this translator owns, which is how the Python model exposes them.
    [[nodiscard]] NmtStats stats() const;

private:
    mutable CorpusStore* corpus_;
    NmtEngine* engine_;
    NmtTranslatorOptions options_;
    //: protected/lost counters only; the engine owns calls/sentences/time.
    mutable NmtStats term_stats_;
};

}  // namespace watashi
