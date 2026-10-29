#include "watashi/translator.hpp"

#include <map>
#include <string>

namespace watashi {

Translation FakeTranslator::translate(
    std::string_view source,
    const TranslateContext& context) const {
    (void)context;
    // Two known sentences plus a visible fallback. No model, no corpus, no
    // network: what matters for the skeleton is that the same input always
    // produces the same output.
    static const std::map<std::string, std::string, std::less<>> kKnown = {
        {"the sword intent of this sect is a myth",
         "this sect's sword intent is only a legend"},
        {"gg wp noob", "good game, well played, beginner"},
    };
    Translation result;
    const auto found = kKnown.find(source);
    result.target = found != kKnown.end() ? found->second : "[fake] " + std::string(source);
    result.backend = "fake";
    result.coverage = 1.0;
    result.confidence = 1.0;
    result.trace = "fake: skeleton path, no OCR and no model";
    return result;
}

}  // namespace watashi
