// The translation seam. Same interface for the deterministic fake, the
// corpus+rules engine and (later) a model: the pipeline never learns which one
// answered, and every backend reports provenance the same way.

#pragma once

#include <string>
#include <string_view>

#include "watashi/corpus.hpp"

namespace watashi {

/// What a translation may depend on besides the text.
struct TranslateContext {
    std::string target_lang = "zh-CN";
    std::string scene;
    std::string window;
};

/// One translated line plus the facts every surface shows.
struct Translation {
    std::string target;
    std::string backend;
    double coverage = 1.0;
    double confidence = 1.0;
    std::string trace;
};

class Translator {
public:
    virtual ~Translator() = default;
    [[nodiscard]] virtual Translation translate(
        std::string_view source,
        const TranslateContext& context) const = 0;
};

/// Deterministic stand-in: a tiny lookup table plus an echo-with-prefix
/// fallback. No model, no network, no randomness.
class FakeTranslator final : public Translator {
public:
    [[nodiscard]] Translation translate(
        std::string_view source,
        const TranslateContext& context) const override;
};

/// Corpus + rules only. Fully local and model-free; the target/scene/window
/// come from the caller's TranslateContext.
class CorpusTranslator final : public Translator {
public:
    explicit CorpusTranslator(CorpusStore& corpus) : corpus_(&corpus) {}

    [[nodiscard]] Translation translate(
        std::string_view source,
        const TranslateContext& context) const override;

    [[nodiscard]] const CorpusStore& corpus() const { return *corpus_; }

private:
    // Non-const on purpose: a lookup checks the files for changes, and that is
    // a state change on the store rather than something the call may skip.
    CorpusStore* corpus_;
};

}  // namespace watashi
