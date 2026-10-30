// The corpus-first NMT path, ported from local_nmt.translate_readable_batch_with_nmt.
//
// The decision order is the whole point of this file, so it is spelled out once
// and every branch returns a Translation carrying the same fields the Python
// Refinement carries: backend, protected_terms, lost_placeholders, used_model and
// fallback_reason.

#include "watashi/nmt.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <map>
#include <string>
#include <utility>
#include <vector>

#include "corpus/text_utf8.hpp"

namespace watashi {
namespace {

constexpr double kMinResidualWords = 2.0;  // MIN_RESIDUAL_WORDS

double now_ms() {
    return std::chrono::duration<double, std::milli>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

bool unspaced_target(const std::string& nllb_code) {
    const auto base = nllb_code.substr(0, nllb_code.find('_'));
    return base == "zho" || base == "jpn" || base == "kor" || base == "tha";
}

std::string nllb_backend(const NmtEngine& engine, bool protected_path) {
    std::string backend = "local-nmt/" + engine.compute_type();
    if (protected_path) {
        backend += "/protected";
    }
    return backend;
}

Translation corpus_result(std::string_view text, const Outcome& outcome) {
    Translation result;
    result.target = outcome.target_text;
    result.backend = "corpus+rules";
    result.coverage = outcome.coverage();
    result.confidence = outcome.confidence();
    result.used_model = false;
    result.trace = result.backend;
    (void)text;
    return result;
}

Translation fallback(std::string_view text, const Outcome& outcome, const std::string& reason,
                     int terms = 0, int lost = 0) {
    Translation result = corpus_result(text, outcome);
    result.protected_terms = terms;
    result.lost_placeholders = lost;
    result.fallback_reason = reason;
    result.trace = result.backend + " (" + reason + ")";
    return result;
}

Translation model_result(const NmtEngine& engine, const std::string& target, bool protected_path,
                         int terms) {
    Translation result;
    result.target = target;
    result.backend = nllb_backend(engine, protected_path);
    result.coverage = 1.0;
    result.confidence = 0.75;
    result.used_model = true;
    result.protected_terms = terms;
    result.trace = result.backend;
    return result;
}

}  // namespace

NmtTranslator::NmtTranslator(CorpusStore& corpus, NmtEngine& engine, NmtTranslatorOptions options)
    : corpus_(&corpus), engine_(&engine), options_(std::move(options)) {}

NmtStats NmtTranslator::stats() const {
    NmtStats merged = engine_->stats();
    merged.protected_terms = term_stats_.protected_terms;
    merged.lost_placeholders = term_stats_.lost_placeholders;
    return merged;
}

Translation NmtTranslator::translate(std::string_view source, const TranslateContext& context) const {
    const std::vector<Translation> results =
        translate_batch({std::string(source)}, context);
    if (results.empty()) {
        return Translation{};
    }
    return results.front();
}

std::vector<Translation> NmtTranslator::translate_batch(
    const std::vector<std::string>& texts, const TranslateContext& context) const {
    using namespace nmt_detail;

    if (texts.empty()) {
        return {};
    }

    const std::string& target_lang = context.target_lang;
    std::string nllb_target;
    bool have_target = true;
    try {
        nllb_target = to_nllb_code(target_lang);
    } catch (const std::exception&) {
        have_target = false;
    }

    std::vector<Translation> results(texts.size());
    std::vector<Outcome> outcomes;
    outcomes.reserve(texts.size());
    const Context corpus_context{target_lang, context.scene, context.window};
    for (const auto& text : texts) {
        outcomes.push_back(corpus_->translate(text, target_lang, corpus_context));
    }

    // Lines that need the model, with the source-language bucket they belong to.
    std::vector<std::size_t> pending;
    std::vector<std::string> codes(texts.size());
    for (std::size_t index = 0; index < texts.size(); ++index) {
        const std::string& text = texts[index];
        const Outcome& outcome = outcomes[index];
        if (is_blank(text)) {
            results[index] = fallback(text, outcome, "empty input");
            continue;
        }
        if (corpus_is_complete(outcome, target_lang)) {
            results[index] = corpus_result(text, outcome);
            results[index].fallback_reason = "already complete through corpus+rules";
            results[index].trace = results[index].backend + " (already complete)";
            continue;
        }
        if (!have_target) {
            results[index] =
                fallback(text, outcome, "no NLLB code for target '" + target_lang + "'");
            continue;
        }
        if (!engine_->available()) {
            const std::string error = engine_->load_error();
            results[index] =
                fallback(text, outcome, error.empty() ? "model unavailable" : error);
            continue;
        }

        std::string code;
        const std::string declared = options_.source_lang;
        const bool declared_language = !declared.empty() && text::lower_ascii(declared) != "auto";
        if (declared_language) {
            try {
                code = to_nllb_code(declared);
            } catch (const std::exception&) {
                code = detect_language(text);
            }
        } else {
            code = detect_language(text);
        }
        codes[index] = code;
        pending.push_back(index);
    }

    struct PendingLine {
        std::size_t index;
        Translation candidate;
    };

    if (!pending.empty()) {
        // One language group per source code, first-seen order preserved.
        std::vector<SourceGroup> groups;
        std::map<std::string, std::size_t> positions;
        for (std::size_t index : pending) {
            auto it = positions.find(codes[index]);
            if (it == positions.end()) {
                positions[codes[index]] = groups.size();
                groups.push_back(SourceGroup{codes[index], {}});
                it = positions.find(codes[index]);
            }
            groups[it->second].indices.push_back(index);
        }

        std::vector<PendingLine> retry;
        for (const auto& group : groups) {
            std::vector<std::string> inputs;
            inputs.reserve(group.indices.size());
            for (std::size_t index : group.indices) {
                inputs.push_back(outcomes[index].target_text);
            }

            std::vector<std::string> raws;
            std::string batch_error;
            const double started = now_ms();
            try {
                raws = engine_->translate_batch(inputs, nllb_target, group.code);
            } catch (const std::exception& error) {
                batch_error = error.what();
            }
            const double per_line_ms =
                (now_ms() - started) / std::max<std::size_t>(1, group.indices.size());

            if (!batch_error.empty()) {
                for (std::size_t index : group.indices) {
                    results[index] = fallback(texts[index], outcomes[index],
                                              "model error: " + batch_error);
                }
                continue;
            }

            for (std::size_t offset = 0; offset < group.indices.size(); ++offset) {
                const std::size_t index = group.indices[offset];
                std::string raw = offset < raws.size() ? raws[offset] : std::string{};
                if (unspaced_target(nllb_target)) {
                    raw = tidy_cjk_spacing(raw);
                }
                const std::vector<std::string> targets =
                    term_targets(outcomes[index], options_.protect_min_confidence);
                const std::vector<std::string> lost =
                    targets.empty() ? std::vector<std::string>{}
                                    : missing_terms(raw, targets);
                if (usable_model_text(raw, target_lang) && lost.empty()) {
                    term_stats_.protected_terms += static_cast<int>(targets.size());
                    results[index] = model_result(*engine_, raw, false,
                                                  static_cast<int>(targets.size()));
                    results[index].elapsed_ms = per_line_ms;
                    continue;
                }

                Translation candidate = model_result(*engine_, raw, false,
                                                    static_cast<int>(targets.size()));
                candidate.lost_placeholders = static_cast<int>(lost.size());
                candidate.fallback_reason =
                    lost.empty() ? "model output was not a complete translation"
                                 : std::to_string(lost.size()) + " term(s) not preserved";
                if (is_blank(raw) || is_degenerate(raw)) {
                    candidate.used_model = false;
                }
                candidate.elapsed_ms = per_line_ms;
                term_stats_.protected_terms += static_cast<int>(targets.size());
                term_stats_.lost_placeholders += static_cast<int>(lost.size());
                retry.push_back(PendingLine{index, candidate});
            }
        }

        // Second attempt: mask the terms, which is worse for fluency but cannot
        // lose terminology silently. Failures keep the fluent candidate.
        if (!retry.empty()) {
            std::vector<SourceGroup> retry_groups;
            std::map<std::string, std::size_t> retry_positions;
            std::vector<std::pair<std::size_t, ProtectedText>> prepared;
            for (const auto& item : retry) {
                const ProtectedText protected_text =
                    protect_terms(texts[item.index], outcomes[item.index],
                                  options_.placeholder_scheme,
                                  options_.protect_min_confidence);
                if (protected_text.count() &&
                    static_cast<double>(residual_word_count(protected_text.masked,
                                                            protected_text.pattern)) <
                        kMinResidualWords) {
                    results[item.index] = item.candidate;
                    continue;
                }
                auto it = retry_positions.find(codes[item.index]);
                if (it == retry_positions.end()) {
                    retry_positions[codes[item.index]] = retry_groups.size();
                    retry_groups.push_back(SourceGroup{codes[item.index], {}});
                    it = retry_positions.find(codes[item.index]);
                }
                retry_groups[it->second].indices.push_back(prepared.size());
                prepared.emplace_back(item.index, protected_text);
                // keep the candidate reachable for the failure path
                results[item.index] = item.candidate;
            }

            for (const auto& group : retry_groups) {
                std::vector<std::string> masked;
                masked.reserve(group.indices.size());
                for (std::size_t slot : group.indices) {
                    masked.push_back(prepared[slot].second.masked);
                }
                std::vector<std::string> raws;
                std::string batch_error;
                const double started = now_ms();
                try {
                    raws = engine_->translate_batch(masked, nllb_target, group.code);
                } catch (const std::exception& error) {
                    batch_error = error.what();
                }
                const double per_line_ms =
                    (now_ms() - started) / std::max<std::size_t>(1, group.indices.size());
                if (!batch_error.empty()) {
                    continue;  // candidates already in results
                }
                for (std::size_t offset = 0; offset < group.indices.size(); ++offset) {
                    const std::size_t index = prepared[group.indices[offset]].first;
                    const ProtectedText& protected_text = prepared[group.indices[offset]].second;
                    std::string raw = offset < raws.size() ? raws[offset] : std::string{};
                    if (unspaced_target(nllb_target)) {
                        raw = tidy_cjk_spacing(raw);
                    }
                    const auto [restored, lost] = restore_terms(raw, protected_text);
                    if (lost.empty() && !is_degenerate(restored) &&
                        usable_model_text(restored, nllb_target)) {
                        results[index] =
                            model_result(*engine_, restored, true, protected_text.count());
                        results[index].elapsed_ms = per_line_ms;
                    } else {
                        results[index].elapsed_ms = per_line_ms;
                    }
                }
            }
        }
    }

    return results;
}

}  // namespace watashi
