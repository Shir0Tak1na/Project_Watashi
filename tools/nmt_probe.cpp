// Phase 0 probe: load the NLLB CTranslate2 model from C++ and translate four
// fixed sentences. It exists so the claim "the native engine can run the model"
// is reproducible from the build tree instead of a throwaway spike.
//
//     watashi_nmt_probe [model_dir]
//
// The tokenizer is SentencePiece (the model's original tokenizer). Pieces were
// checked against the Python `tokenizers` output first; only the ids differ, by
// one, because the Hugging Face vocabulary keeps a leading <s> slot. CTranslate2
// consumes token strings, so that offset does not matter.

#include <chrono>
#include <cstdio>
#include <iostream>
#include <string>
#include <vector>

#include <ctranslate2/translator.h>
#include <sentencepiece_processor.h>

namespace {

constexpr const char* kDefaultModelDir =
    "models/llm/nllb-200-distilled-600M-ct2-int8";

double now_ms() {
    return std::chrono::duration<double, std::milli>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

/// Python's `NmtModel.encode`: source language token, pieces, `</s>`.
std::vector<std::string> encode(
    const sentencepiece::SentencePieceProcessor& sp,
    const std::string& text,
    const std::string& source_lang,
    int max_input_length) {
    std::vector<std::string> pieces;
    const auto status = sp.Encode(text, &pieces);
    if (!status.ok()) {
        throw std::runtime_error("SentencePiece encode failed: " + status.ToString());
    }
    std::vector<std::string> tokens;
    tokens.push_back(source_lang);
    tokens.insert(tokens.end(), pieces.begin(), pieces.end());
    if (static_cast<int>(tokens.size()) > max_input_length) {
        tokens.resize(static_cast<std::size_t>(max_input_length));
    }
    tokens.push_back("</s>");
    return tokens;
}

/// Python's `NmtModel.decode`: drop specials, then the Metaspace decoder.
std::string decode(const std::vector<std::string>& tokens, const std::string& target_lang) {
    static const std::string kSpace = "\u2581";
    std::string text;
    for (const auto& token : tokens) {
        if (token == target_lang || token == "<s>" || token == "</s>" || token == "<pad>" ||
            token == "<unk>") {
            continue;
        }
        if (token.rfind(kSpace, 0) == 0) {
            text += ' ';
            text += token.substr(kSpace.size());
        } else {
            text += token;
        }
    }
    const auto first = text.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) {
        return {};
    }
    const auto last = text.find_last_not_of(" \t\r\n");
    return text.substr(first, last - first + 1);
}

}  // namespace

int main(int argc, char** argv) {
    const std::string model_dir = argc > 1 ? argv[1] : kDefaultModelDir;

    sentencepiece::SentencePieceProcessor sp;
    const auto load_status = sp.Load(model_dir + "/sentencepiece.bpe.model");
    if (!load_status.ok()) {
        std::fprintf(stderr, "cannot load the SentencePiece model: %s\n",
                     load_status.ToString().c_str());
        return 2;
    }

    const std::vector<std::string> lines = {
        "the sword intent of this sect is a myth",
        "he broke through to the void realm",
        "gg wp noob",
        "the antidragon superspirit guard blocked the gate",
    };

    const auto load_started = now_ms();
    ctranslate2::ReplicaPoolConfig config;
    config.num_threads_per_replica = 4;
    const bool float32_mode =
        argc > 2 && (std::string(argv[2]) == "--float32" || std::string(argv[2]) == "--float32-single");
    const bool one_by_one =
        argc > 2 && (std::string(argv[2]) == "--single" || std::string(argv[2]) == "--float32-single");
    ctranslate2::Translator translator(
        model_dir, ctranslate2::Device::CPU,
        float32_mode ? ctranslate2::ComputeType::FLOAT32 : ctranslate2::ComputeType::INT8, {0},
        false, config);
    const double load_ms = now_ms() - load_started;

    std::vector<std::vector<std::string>> sources;
    sources.reserve(lines.size());
    for (const auto& line : lines) {
        sources.push_back(encode(sp, line, "eng_Latn", 256));
    }

    ctranslate2::TranslationOptions options;
    options.beam_size = 1;
    options.max_decoding_length = 192;
    options.return_scores = false;

    const std::vector<std::vector<std::string>> prefixes(lines.size(), {"zho_Hans"});
    const double translate_started = now_ms();
    std::vector<ctranslate2::TranslationResult> results;
    if (one_by_one) {
        for (std::size_t index = 0; index < sources.size(); ++index) {
            const std::vector<std::string> prefix = {"zho_Hans"};
            const auto batch = translator.translate_batch({sources[index]}, {prefix}, options);
            results.push_back(batch.front());
        }
    } else {
        results = translator.translate_batch(sources, prefixes, options);
    }
    const double total_ms = now_ms() - translate_started;

    std::printf("model_load_ms=%.1f sentences=%zu total_ms=%.1f\n", load_ms,
                lines.size(), total_ms);
    for (std::size_t index = 0; index < sources.size(); ++index) {
        std::printf("src_tokens[%zu]:", index);
        for (const auto& token : sources[index]) {
            std::printf(" %s", token.c_str());
        }
        std::printf("\n");
    }
    for (std::size_t index = 0; index < results.size(); ++index) {
        const auto& hypothesis = results[index].output();
        const std::string text = decode(hypothesis, "zho_Hans");
        std::printf("src: %s\n", lines[index].c_str());
        std::printf("out: %s\n", text.c_str());
        std::printf("tokens=%zu\n", hypothesis.size());
    }
    return 0;
}
