// CTranslate2 + SentencePiece implementation of the NMT engine.
//
// The model directory is the same one the Python prototype loads. The tokenizer
// is the model's original SentencePiece table; pieces were verified equal to the
// Python ``tokenizers`` output, and only the integer ids differ (the Hugging Face
// vocabulary keeps a leading <s> slot). CTranslate2 takes token strings, so the
// id offset is irrelevant.
//
// Loading never throws: a missing model or a load failure sets ``load_error`` so
// the caller can fall back to corpus+rules, matching ``NmtModel.load``.

#include "watashi/nmt.hpp"

#include <chrono>
#include <cstdio>
#include <filesystem>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

#include <ctranslate2/translator.h>
#include <sentencepiece_processor.h>

namespace watashi {
namespace {

double now_ms() {
    return std::chrono::duration<double, std::milli>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

std::string decode_tokens(const std::vector<std::string>& tokens, const std::string& target_lang) {
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

struct LocalNmt::Impl {
    NmtOptions options;
    NmtStats stats;
    std::mutex mutex;
    sentencepiece::SentencePieceProcessor processor;
    std::unique_ptr<ctranslate2::Translator> translator;
    std::string error;
    double load_ms = 0.0;
};

LocalNmt::LocalNmt(NmtOptions options) : impl_(std::make_unique<Impl>()) {
    impl_->options = std::move(options);
    const std::filesystem::path directory = impl_->options.model_dir;

    std::vector<std::string> missing;
    for (const char* name : {"model.bin", "config.json", "sentencepiece.bpe.model"}) {
        if (!std::filesystem::exists(directory / name)) {
            missing.push_back(name);
        }
    }
    if (!missing.empty()) {
        std::string names;
        for (std::size_t index = 0; index < missing.size(); ++index) {
            if (index) {
                names += ", ";
            }
            names += missing[index];
        }
        impl_->error = "model incomplete at " + directory.string() + " (missing " + names +
                       "); run: prototype\\run.cmd fetch_model";
        return;
    }

    const auto load_started = now_ms();
    const auto status = impl_->processor.Load((directory / "sentencepiece.bpe.model").string());
    if (!status.ok()) {
        impl_->error = "RuntimeError: SentencePiece load failed: " + status.ToString();
        return;
    }

    try {
        ctranslate2::ReplicaPoolConfig config;
        config.num_threads_per_replica = static_cast<std::size_t>(
            impl_->options.intra_threads > 0 ? impl_->options.intra_threads : 4);
        const auto compute_type = ctranslate2::str_to_compute_type(impl_->options.compute_type);
        impl_->translator = std::make_unique<ctranslate2::Translator>(
            directory.string(), ctranslate2::Device::CPU, compute_type,
            std::vector<int>{0}, false, config);
    } catch (const std::exception& error) {
        impl_->error = std::string("RuntimeError: ") + error.what();
        impl_->translator.reset();
        return;
    }
    impl_->load_ms = now_ms() - load_started;
}

LocalNmt::~LocalNmt() = default;
LocalNmt::LocalNmt(LocalNmt&&) noexcept = default;
LocalNmt& LocalNmt::operator=(LocalNmt&&) noexcept = default;

bool LocalNmt::available() const {
    return impl_->translator != nullptr;
}

std::string LocalNmt::load_error() const {
    return impl_->error;
}

std::string LocalNmt::compute_type() const {
    return impl_->options.compute_type;
}

double LocalNmt::load_ms() const {
    return impl_->load_ms;
}

const NmtStats& LocalNmt::stats() const {
    return impl_->stats;
}

std::vector<std::string> LocalNmt::encode_tokens(const std::vector<std::string>& pieces,
                                                 const std::string& source_lang,
                                                 int max_input_length) {
    std::vector<std::string> tokens;
    tokens.reserve(pieces.size() + 2);
    tokens.push_back(source_lang);
    tokens.insert(tokens.end(), pieces.begin(), pieces.end());
    if (max_input_length > 0 && static_cast<int>(tokens.size()) > max_input_length) {
        tokens.resize(static_cast<std::size_t>(max_input_length));
    }
    tokens.push_back("</s>");
    return tokens;
}

std::vector<std::string> LocalNmt::translate_batch(const std::vector<std::string>& texts,
                                                   const std::string& target_nllb,
                                                   const std::string& source_nllb) {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    if (impl_->translator == nullptr) {
        throw std::runtime_error(impl_->error.empty() ? "model not loaded" : impl_->error);
    }
    if (texts.empty()) {
        return {};
    }

    std::vector<std::vector<std::string>> sources;
    sources.reserve(texts.size());
    for (const auto& text : texts) {
        std::vector<std::string> pieces;
        const auto status = impl_->processor.Encode(text, &pieces);
        if (!status.ok()) {
            throw std::runtime_error("SentencePiece encode failed: " + status.ToString());
        }
        sources.push_back(encode_tokens(pieces, source_nllb, impl_->options.max_input_length));
    }

    ctranslate2::TranslationOptions options;
    options.beam_size = static_cast<std::size_t>(std::max(1, impl_->options.beam_size));
    options.max_decoding_length = static_cast<std::size_t>(
        std::max(1, impl_->options.max_decoding_length));
    options.return_scores = false;

    const std::vector<std::vector<std::string>> prefixes(texts.size(), {target_nllb});
    const double started = now_ms();
    std::vector<ctranslate2::TranslationResult> results;
    try {
        results = impl_->translator->translate_batch(sources, prefixes, options);
    } catch (...) {
        impl_->stats.errors += 1;
        throw;
    }
    const double elapsed = now_ms() - started;
    impl_->stats.calls += 1;
    impl_->stats.sentences += static_cast<int>(texts.size());
    impl_->stats.total_ms += elapsed;

    std::vector<std::string> outputs;
    outputs.reserve(results.size());
    for (const auto& result : results) {
        outputs.push_back(result.hypotheses.empty()
                              ? std::string{}
                              : decode_tokens(result.hypotheses.front(), target_nllb));
    }
    return outputs;
}

}  // namespace watashi
