#include "translator_core.h"
#include <cstring>
#include <string>

int run_translation_core(const struct TranslationRequest *request, struct TranslationResponse *response) {
    if (request == nullptr || response == nullptr) {
        return 0;
    }

    std::string source = request->source_lang;
    std::string target = request->target_lang;
    std::string text = request->text;

    if (text.empty()) {
        std::strncpy(response->translated_text, "", sizeof(response->translated_text));
        response->success = 0;
        return 0;
    }

    std::string result = "[C Core] " + text;
    if (source != target) {
        result = "[C Core translated] " + text;
    }

    std::strncpy(response->translated_text, result.c_str(), sizeof(response->translated_text) - 1);
    response->translated_text[sizeof(response->translated_text) - 1] = '\0';
    response->success = 1;
    return 1;
}
