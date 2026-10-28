#include "watashi/pipeline.hpp"

#include <map>
#include <string>

namespace watashi {

std::string FakeTranslator::translate(std::string_view source) const {
    // Two known sentences plus a visible fallback. No model, no corpus, no
    // network: what matters for the skeleton is that the same input always
    // produces the same output.
    static const std::map<std::string, std::string, std::less<>> kKnown = {
        {"the sword intent of this sect is a myth",
         "this sect's sword intent is only a legend"},
        {"gg wp noob", "good game, well played, beginner"},
    };
    const auto found = kKnown.find(source);
    if (found != kKnown.end()) {
        return found->second;
    }
    return "[fake] " + std::string(source);
}

}  // namespace watashi
