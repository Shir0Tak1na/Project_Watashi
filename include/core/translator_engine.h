#pragma once

#include <string>
#include <vector>

namespace projectwatashi {

struct TranslationRequest {
    std::string source_lang;
    std::string target_lang;
    std::string text;
};

struct TranslationResult {
    std::string original_text;
    std::string translated_text;
    bool success = false;
    std::vector<std::string> warnings;
};

class TranslatorEngine {
public:
    TranslatorEngine();
    ~TranslatorEngine();

    TranslationResult translate(const TranslationRequest& request) const;
};

} // namespace projectwatashi
