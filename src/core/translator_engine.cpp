#include "core/translator_engine.h"

#include <iostream>

namespace projectwatashi {

TranslatorEngine::TranslatorEngine() = default;
TranslatorEngine::~TranslatorEngine() = default;

TranslationResult TranslatorEngine::translate(const TranslationRequest& request) const {
    TranslationResult result;
    result.original_text = request.text;
    result.success = !request.text.empty();

    if (!result.success) {
        return result;
    }

    result.translated_text = "[local-rule-translation] " + request.text;
    return result;
}

} // namespace projectwatashi
