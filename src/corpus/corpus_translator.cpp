#include "watashi/translator.hpp"

namespace watashi {

Translation CorpusTranslator::translate(
    std::string_view source,
    const TranslateContext& context) const {
    const Context corpus_context{context.target_lang, context.scene, context.window};
    const Outcome outcome = corpus_->translate(source, context.target_lang, corpus_context);

    Translation result;
    result.target = outcome.target_text;
    result.backend = outcome.backend;
    result.coverage = outcome.coverage();
    result.confidence = outcome.confidence();
    result.trace = outcome.trace();
    return result;
}

}  // namespace watashi
