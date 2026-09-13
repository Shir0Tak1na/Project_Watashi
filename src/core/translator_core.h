#ifndef TRANSLATOR_CORE_H
#define TRANSLATOR_CORE_H

#ifdef __cplusplus
extern "C" {
#endif

struct TranslationRequest {
    char source_lang[16];
    char target_lang[16];
    char text[2048];
};

struct TranslationResponse {
    char translated_text[2048];
    int success;
};

int run_translation_core(const struct TranslationRequest *request, struct TranslationResponse *response);

#ifdef __cplusplus
}
#endif

#endif
