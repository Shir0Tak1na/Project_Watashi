#include <catch2/catch_test_macros.hpp>

#include <cstdint>
#include <string>

#include "temp_corpus.hpp"
#include "watashi/corpus.hpp"
#include "watashi/rules.hpp"

using namespace watashi;
using watashi::test::options_for;
using watashi::test::TempCorpus;

namespace {

std::u32string U(const std::string& text) {
    std::u32string out;
    for (std::size_t index = 0; index < text.size();) {
        const auto lead = static_cast<unsigned char>(text[index]);
        char32_t codepoint = lead;
        std::size_t length = 1;
        if ((lead & 0xE0) == 0xC0) {
            codepoint = lead & 0x1F;
            length = 2;
        } else if ((lead & 0xF0) == 0xE0) {
            codepoint = lead & 0x0F;
            length = 3;
        } else if ((lead & 0xF8) == 0xF0) {
            codepoint = lead & 0x07;
            length = 4;
        }
        for (std::size_t offset = 1; offset < length; ++offset) {
            codepoint = (codepoint << 6) |
                        (static_cast<unsigned char>(text[index + offset]) & 0x3F);
        }
        out.push_back(codepoint);
        index += length;
    }
    return out;
}

CorpusStore store_with_rules(
    const TempCorpus& corpus,
    const std::string& rules_json,
    const std::string& entries = R"({"gravity": "重力", "gg": "打得好", "cyber": "赛博", "punk": "朋克", "金丹": {"target": "golden core", "lang": "en"}})") {
    corpus.write("general/g.json", entries);
    corpus.write("rules/r.json", rules_json);
    auto options = options_for(corpus);
    options.rule_files = {corpus.path("rules/r.json")};
    return CorpusStore(options);
}

}  // namespace

TEST_CASE("an affix rule translates the stem and re-attaches the prefix") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "affix", "type": "affix", "priority": 10, "confidence": 0.55,
         "target": "zh-CN", "min_stem": 3,
         "prefixes": {"anti": "反"}, "suffixes": {"ness": "性"}}]})json");

    const Outcome outcome = store.translate("antigravity", "zh-CN");
    REQUIRE(outcome.target_text == "反重力");
    REQUIRE(outcome.spans.front().origin == "rule:affix");
    REQUIRE(outcome.spans.front().rule_id == "affix");
}

TEST_CASE("an affix rule also works on suffixes") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "affix", "type": "affix", "target": "zh-CN", "min_stem": 3,
         "suffixes": {"ness": "性"}}]})json");

    REQUIRE(store.translate("gravityness", "zh-CN").target_text == "重力性");
}

TEST_CASE("a stem shorter than min_stem is not stripped") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "affix", "type": "affix", "target": "zh-CN", "min_stem": 3,
         "prefixes": {"un": "不"}}]})json");

    REQUIRE(store.translate("ungg", "zh-CN").target_text == "ungg");
}

TEST_CASE("an affix rule needs a corpus entry for the stem") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "affix", "type": "affix", "target": "zh-CN", "min_stem": 3,
         "prefixes": {"anti": "反"}}]})json");

    REQUIRE(store.translate("antiunknown", "zh-CN").target_text == "antiunknown");
}

TEST_CASE("a morpheme rule splits an unknown word into known pieces") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "morpheme", "type": "morpheme", "target": "zh-CN", "separator": ""}]})json");

    REQUIRE(store.translate("cyberpunk", "zh-CN").target_text == "赛博朋克");
}

TEST_CASE("a morpheme rule honours its separator") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "morpheme", "type": "morpheme", "target": "zh-CN", "separator": " "}]})json");

    REQUIRE(store.translate("cyberpunk", "zh-CN").target_text == "赛博 朋克");
}

TEST_CASE("a single morpheme is not a decomposition") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "morpheme", "type": "morpheme", "target": "zh-CN"}]})json");

    REQUIRE_FALSE(store.rules().apply(U("cyber"), store, "zh-CN").has_value());
}

TEST_CASE("a template rule translates its named group through the corpus") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "realm", "type": "template", "priority": 20, "confidence": 0.6,
         "target": "en", "pattern": "(?P<base>.+)(境|界)", "replacement": "{base} Realm"}]})json");

    const auto hit = store.rules().apply(U("金丹境"), store, "en");
    REQUIRE(hit.has_value());
    REQUIRE(hit->target == "golden core Realm");
    REQUIRE(hit->confidence == 0.6);
    REQUIRE(hit->rule_id == "realm");
}

TEST_CASE("a template rule keeps an unknown base as written") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "realm", "type": "template", "target": "en",
         "pattern": "(?P<base>.+)(境|界)", "replacement": "{base} Realm"}]})json");

    const auto hit = store.rules().apply(U("玄天境"), store, "en");
    REQUIRE(hit.has_value());
    REQUIRE(hit->target == "玄天 Realm");
}

TEST_CASE("a template rule with no match returns nothing") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "realm", "type": "template", "target": "en",
         "pattern": "(?P<base>.+)(境|界)", "replacement": "{base} Realm"}]})json");

    REQUIRE_FALSE(store.rules().apply(U("sword"), store, "en").has_value());
}

TEST_CASE("a template rule with an invalid regex is disabled") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "broken", "type": "template", "pattern": "(?P<base>", "replacement": "{base}"}]})json");

    REQUIRE(store.rule_count() == 0);
    REQUIRE_FALSE(store.rules().apply(U("sword"), store, "en").has_value());
}

TEST_CASE("a transliterate rule keeps the token") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "tr", "type": "transliterate", "priority": 999, "confidence": 0.1}]})json");

    const auto hit = store.rules().apply(U("Zorblax"), store, "en");
    REQUIRE(hit.has_value());
    REQUIRE(hit->target == "Zorblax");
    REQUIRE(hit->confidence == 0.1);
}

TEST_CASE("a transliterate rule refuses a blank token") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "tr", "type": "transliterate"}]})json");

    REQUIRE_FALSE(store.rules().apply(U("   "), store, "en").has_value());
}

TEST_CASE("a rule language filter keeps it out of other targets") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "affix", "type": "affix", "target": "zh-CN", "min_stem": 3,
         "prefixes": {"anti": "反"}}]})json");

    REQUIRE(store.rules().apply(U("antigravity"), store, "zh-CN").has_value());
    REQUIRE_FALSE(store.rules().apply(U("antigravity"), store, "en").has_value());
}

TEST_CASE("rules run in priority order") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "late", "type": "template", "priority": 50, "pattern": "X+", "replacement": "late"},
        {"id": "early", "type": "template", "priority": 1, "pattern": "X+", "replacement": "early"}]})json");

    const auto hit = store.rules().apply(U("XXX"), store, "en");
    REQUIRE(hit.has_value());
    REQUIRE(hit->rule_id == "early");
    REQUIRE(hit->target == "early");
}

TEST_CASE("a disabled rule never runs") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "off", "type": "transliterate", "enabled": false},
        {"id": "on", "type": "transliterate", "priority": 5}]})json");

    REQUIRE(store.rule_count() == 1);
    const auto hit = store.rules().apply(U("Xylo"), store, "en");
    REQUIRE(hit.has_value());
    REQUIRE(hit->rule_id == "on");
}

TEST_CASE("an unknown rule type is skipped without failing the file") {
    TempCorpus corpus;
    auto store = store_with_rules(corpus, R"json({"rules": [
        {"id": "mystery", "type": "quantum"},
        {"id": "tr", "type": "transliterate"}]})json");

    REQUIRE(store.rule_count() == 1);
    REQUIRE(store.rule_ids().front() == "tr");
}

TEST_CASE("corpus translation still works with no rule file at all") {
    TempCorpus corpus;
    corpus.write("general/g.json", R"({"sword": "剑"})");
    CorpusStore store(options_for(corpus));

    REQUIRE(store.rule_count() == 0);
    REQUIRE(store.translate("sword", "zh-CN").target_text == "剑");
}
