#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <cstddef>
#include <memory>
#include <string>
#include <vector>

#include "temp_corpus.hpp"
#include "watashi/nmt.hpp"

using watashi::NmtEngine;
using watashi::NmtOptions;
using watashi::NmtStats;
using watashi::NmtTranslator;
using watashi::NmtTranslatorOptions;
using watashi::Outcome;
using watashi::Span;
using watashi::TranslateContext;
using watashi::CorpusStore;
using watashi::nmt_detail::ProtectedText;
using namespace watashi::nmt_detail;
using watashi::test::TempCorpus;
using watashi::test::options_for;

namespace {

/// A scripted model: one response vector per call, in call order.
class ScriptNmt final : public NmtEngine {
public:
    bool ok = true;
    std::string error;
    std::string type = "int8";
    double load = 12.0;
    std::vector<std::vector<std::string>> responses;
    std::vector<std::pair<std::string, std::vector<std::string>>> calls;

    [[nodiscard]] bool available() const override { return ok; }
    [[nodiscard]] std::string load_error() const override { return error; }
    [[nodiscard]] std::string compute_type() const override { return type; }
    [[nodiscard]] double load_ms() const override { return load; }

    [[nodiscard]] std::vector<std::string> translate_batch(
        const std::vector<std::string>& texts, const std::string&, const std::string& source) override {
        calls.emplace_back(source, texts);
        if (!ok) {
            throw std::runtime_error(error.empty() ? "model unavailable" : error);
        }
        if (responses.empty()) {
            return std::vector<std::string>(texts.size(), "");
        }
        const std::size_t index = std::min(calls.size() - 1, responses.size() - 1);
        std::vector<std::string> output = responses[index];
        output.resize(texts.size());
        return output;
    }

    [[nodiscard]] const NmtStats& stats() const override { return stats_; }

    NmtStats stats_;
};

struct Fixture {
    TempCorpus corpus;
    std::unique_ptr<CorpusStore> store;

    explicit Fixture(const std::string& entries = R"({"sword": "剑意", "sect": "宗门"})") {
        corpus.write("general/g.json", entries);
        store = std::make_unique<CorpusStore>(options_for(corpus));
    }
};

TranslateContext chinese() {
    return TranslateContext{"zh-CN", "", ""};
}

}  // namespace

TEST_CASE("the language code table matches the Python one") {
    REQUIRE(to_nllb_code("zh-CN") == "zho_Hans");
    REQUIRE(to_nllb_code("zh-TW") == "zho_Hant");
    REQUIRE(to_nllb_code("en") == "eng_Latn");
    REQUIRE(to_nllb_code("zho_Hans") == "zho_Hans");
    REQUIRE_THROWS_AS(to_nllb_code("klingon"), std::invalid_argument);
}

TEST_CASE("script detection follows the Python rules") {
    REQUIRE(detect_language("the sword intent") == "eng_Latn");
    REQUIRE(detect_language("他突破了虚空境界") == "zho_Hans");
    REQUIRE(detect_language("これはテストです") == "jpn_Jpan");
    REQUIRE(detect_language("42", "") == "");
    REQUIRE(detect_language("42") == "eng_Latn");
}

TEST_CASE("has_translatable_content ignores digits and punctuation") {
    REQUIRE_FALSE(has_translatable_content("42"));
    REQUIRE_FALSE(has_translatable_content("..."));
    REQUIRE(has_translatable_content("abc"));
    REQUIRE(has_translatable_content("剑"));
}

TEST_CASE("matches_target compares language bases, not scripts") {
    REQUIRE(matches_target("他突破了虚空境界", "zh-CN"));
    REQUIRE(matches_target("他突破了虚空境界", "zho_Hans"));
    REQUIRE_FALSE(matches_target("the sword intent", "zh-CN"));
    REQUIRE_FALSE(matches_target("", "zh-CN"));
}

TEST_CASE("degenerate output is detected like Python") {
    REQUIRE_FALSE(is_degenerate("这一宗派的剑意是个神话."));
    REQUIRE(is_degenerate(std::string(30, 'q')));
    REQUIRE(is_degenerate("子子子子子子子子子子子子子子子"));
    REQUIRE_FALSE(is_degenerate("短"));
}

TEST_CASE("CJK spacing is tidied but Latin spacing is kept") {
    REQUIRE(tidy_cjk_spacing("对于此 宗门的 剑意是个神话") == "对于此宗门的剑意是个神话");
    REQUIRE(tidy_cjk_spacing("英文 and 中文") == "英文 and 中文");
    REQUIRE(tidy_cjk_spacing("no spaces here") == "no spaces here");
}

TEST_CASE("corpus terms become numbered placeholders") {
    Fixture fixture;
    const Outcome outcome =
        fixture.store->translate("the sword intent of this sect is a myth", "zh-CN");
    const ProtectedText protected_text = protect_terms(
        "the sword intent of this sect is a myth", outcome);
    REQUIRE(protected_text.count() == 2);
    REQUIRE(protected_text.masked.find("\u22640\u2265") != std::string::npos);
    REQUIRE(protected_text.masked.find("\u22641\u2265") != std::string::npos);
    REQUIRE(protected_text.terms.front().second == "剑意");
    REQUIRE(residual_word_count(protected_text.masked) >= 2);
}

TEST_CASE("restoring placeholders reports the ones that vanished") {
    ProtectedText protected_text;
    protected_text.terms = {{"\u22640\u2265", "剑意"}, {"\u22641\u2265", "宗门"}};

    const auto [restored, lost] = restore_terms("这个\u22640\u2265的宗门是个神话", protected_text);
    REQUIRE(restored == "这个剑意的宗门是个神话");
    REQUIRE(lost.size() == 1);
    REQUIRE(lost.front() == "\u22641\u2265");
}

TEST_CASE("residual words decide whether the model has context") {
    REQUIRE(residual_word_count("\u22640\u2265 \u22641\u2265") == 0);
    REQUIRE(residual_word_count("the \u22640\u2265 realm") == 2);
}

TEST_CASE("term targets ignore whitespace when checking survival") {
    const std::vector<std::string> targets = {"剑意", "虚 空"};
    REQUIRE(missing_terms("这个剑意的虚空境界", targets).empty());
    const auto missing = missing_terms("这个故事", targets);
    REQUIRE(missing.size() == 2);
}

TEST_CASE("the corpus is complete only when nothing is left to translate") {
    Outcome chinese;
    chinese.target_text = "龙";
    chinese.spans.push_back(Span{"dragon", "龙", "corpus:g", 0.99, ""});
    REQUIRE(corpus_is_complete(chinese, "zh-CN"));

    Outcome mixed;
    mixed.target_text = "the 剑 intent of this sect is a myth";
    mixed.spans.push_back(Span{"sword", "剑", "corpus:g", 0.99, ""});
    REQUIRE_FALSE(corpus_is_complete(mixed, "zh-CN"));

    Outcome empty;
    REQUIRE_FALSE(corpus_is_complete(empty, "zh-CN"));
}

TEST_CASE("an already complete corpus answer never reaches the model") {
    Fixture fixture(R"({"dragon": "龙"})");
    ScriptNmt model;
    NmtTranslator translator(*fixture.store, model);

    const auto result = translator.translate("dragon", chinese());
    REQUIRE(model.calls.empty());
    REQUIRE(result.backend == "corpus+rules");
    REQUIRE(result.target == "龙");
    REQUIRE_FALSE(result.used_model);
    REQUIRE(result.fallback_reason == "already complete through corpus+rules");
}

TEST_CASE("an empty line falls back without calling the model") {
    Fixture fixture;
    ScriptNmt model;
    NmtTranslator translator(*fixture.store, model);

    const auto result = translator.translate("   ", chinese());
    REQUIRE(model.calls.empty());
    REQUIRE(result.backend == "corpus+rules");
    REQUIRE_FALSE(result.used_model);
    REQUIRE(result.fallback_reason == "empty input");
}

TEST_CASE("an unavailable model reports its load error") {
    Fixture fixture;
    ScriptNmt model;
    model.ok = false;
    model.error = "model incomplete at models/x (missing model.bin)";
    NmtTranslator translator(*fixture.store, model);

    const auto result =
        translator.translate("the sword intent of this sect is a myth", chinese());
    REQUIRE(model.calls.empty());
    REQUIRE_FALSE(result.used_model);
    REQUIRE(result.backend == "corpus+rules");
    REQUIRE(result.fallback_reason == model.error);
}

TEST_CASE("an unknown target language is a reported fallback") {
    Fixture fixture;
    ScriptNmt model;
    NmtTranslator translator(*fixture.store, model);

    const auto result = translator.translate(
        "the sword intent of this sect is a myth", TranslateContext{"klingon", "", ""});
    REQUIRE_FALSE(result.used_model);
    REQUIRE(result.fallback_reason == "no NLLB code for target 'klingon'");
}

TEST_CASE("a code-switched translation that keeps the terms is used") {
    Fixture fixture;
    ScriptNmt model;
    model.responses = {{"这一宗门的剑意是一个神话"}};
    NmtTranslator translator(*fixture.store, model);

    const auto result =
        translator.translate("the sword intent of this sect is a myth", chinese());
    REQUIRE(model.calls.size() == 1);
    REQUIRE(result.used_model);
    REQUIRE(result.backend == "local-nmt/int8");
    REQUIRE(result.target == "这一宗门的剑意是一个神话");
    REQUIRE(result.protected_terms == 2);
    REQUIRE(result.lost_placeholders == 0);
    REQUIRE(result.fallback_reason.empty());
}

TEST_CASE("a lost term retries with placeholders and can succeed") {
    Fixture fixture;
    ScriptNmt model;
    model.responses = {
        {"这一宗派的剑意是一个神话"},                     // loses 宗门
        {"这个\u22640\u2265的\u22641\u2265是一个神话"},  // masked retry restores both
    };
    NmtTranslator translator(*fixture.store, model);

    const auto result =
        translator.translate("the sword intent of this sect is a myth", chinese());
    REQUIRE(model.calls.size() == 2);
    REQUIRE(model.calls[0].second.front() == "the 剑意 intent of this 宗门 is a myth");
    REQUIRE(model.calls[1].second.front().find("\u22640\u2265") != std::string::npos);
    REQUIRE(result.used_model);
    REQUIRE(result.backend == "local-nmt/int8/protected");
    REQUIRE(result.target == "这个剑意的宗门是一个神话");
    REQUIRE(result.protected_terms == 2);
    REQUIRE(result.lost_placeholders == 0);
}

TEST_CASE("when both passes lose terms the fluent sentence is kept and reported") {
    Fixture fixture;
    ScriptNmt model;
    model.responses = {{"这个故事是一个神话"}, {"这个故事是一个神话"}};
    NmtTranslator translator(*fixture.store, model);

    const auto result =
        translator.translate("the sword intent of this sect is a myth", chinese());
    REQUIRE(result.used_model);
    REQUIRE(result.backend == "local-nmt/int8");
    REQUIRE(result.target == "这个故事是一个神话");
    REQUIRE(result.lost_placeholders == 2);
    REQUIRE(result.fallback_reason == "2 term(s) not preserved");
    // never the half-English corpus string
    REQUIRE(result.target.find("intent") == std::string::npos);
}

TEST_CASE("a degenerate model answer is not marked as used") {
    Fixture fixture;
    ScriptNmt model;
    model.responses = {{std::string(40, 'q')}, {std::string(40, 'q')}};
    NmtTranslator translator(*fixture.store, model);

    const auto result =
        translator.translate("the sword intent of this sect is a myth", chinese());
    REQUIRE_FALSE(result.used_model);
    REQUIRE(result.backend == "local-nmt/int8");
    REQUIRE(result.lost_placeholders == 2);
    REQUIRE(result.fallback_reason == "2 term(s) not preserved");
}

TEST_CASE("a whole frame is one model call per source language") {
    Fixture fixture;
    ScriptNmt model;
    model.responses = {{"他突破了虚空境界", "这一宗门的剑意是一个神话"}};
    NmtTranslator translator(*fixture.store, model);

    const std::vector<watashi::Translation> results = translator.translate_batch(
        {"he broke through to the void realm", "the sword intent of this sect is a myth"},
        chinese());
    // Both lines are English, so the model sees one batched call with two inputs.
    REQUIRE(model.calls.size() == 1);
    REQUIRE(model.calls[0].second.size() == 2);
    REQUIRE(results.size() == 2);
    REQUIRE(results[0].used_model);
    REQUIRE(results[1].used_model);
}

TEST_CASE("batch splitting groups lines by source language") {
    const auto groups = group_by_source(
        {"the sword intent", "他突破了虚空境界", "he broke through"}, "auto");
    REQUIRE(groups.size() == 2);
    REQUIRE(groups[0].code == "eng_Latn");
    REQUIRE(groups[0].indices == std::vector<std::size_t>{0, 2});
    REQUIRE(groups[1].code == "zho_Hans");
    REQUIRE(groups[1].indices == std::vector<std::size_t>{1});
}

TEST_CASE("a declared source language overrides detection") {
    const auto groups = group_by_source({"the sword intent"}, "fr");
    REQUIRE(groups.size() == 1);
    REQUIRE(groups[0].code == "fra_Latn");
}

TEST_CASE("an explicit source language that is not an NLLB code falls back to detection") {
    const auto groups = group_by_source({"the sword intent"}, "klingon");
    REQUIRE(groups.size() == 1);
    REQUIRE(groups[0].code == "eng_Latn");
}

TEST_CASE("the translator counts protected terms and losses") {
    Fixture fixture;
    ScriptNmt model;
    model.responses = {{"这个故事是一个神话"}, {"这个故事是一个神话"}};
    NmtTranslator translator(*fixture.store, model);

    (void)translator.translate("the sword intent of this sect is a myth", chinese());
    const NmtStats stats = translator.stats();
    REQUIRE(stats.protected_terms == 2);
    REQUIRE(stats.lost_placeholders == 2);
}
