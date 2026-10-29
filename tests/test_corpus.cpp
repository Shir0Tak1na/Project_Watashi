#include <catch2/catch_test_macros.hpp>

#include <chrono>
#include <string>
#include <thread>
#include <vector>

#include "temp_corpus.hpp"
#include "watashi/corpus.hpp"

using namespace watashi;
using watashi::test::options_for;
using watashi::test::TempCorpus;

namespace {

Outcome run(CorpusStore& store, const std::string& text,
            const std::string& target = "zh-CN", const Context& context = {}) {
    return store.translate(text, target, context);
}

}  // namespace

TEST_CASE("user beats domain and domain beats general") {
    TempCorpus corpus;
    corpus.write("general/g.json", R"({"sword": "剑"})");
    corpus.write("domain/d.json", R"({"sword": {"target": "剑意"}})");
    corpus.write("user/u.json", R"({"sword": "本门之剑"})");
    CorpusStore store(options_for(corpus));

    REQUIRE(run(store, "sword").target_text == "本门之剑");
}

TEST_CASE("domain wins over general when no user entry exists") {
    TempCorpus corpus;
    corpus.write("general/g.json", R"({"sword": "剑"})");
    corpus.write("domain/d.json", R"({"sword": {"target": "剑意"}})");
    CorpusStore store(options_for(corpus));

    REQUIRE(run(store, "sword").target_text == "剑意");
}

TEST_CASE("priority decides inside one layer") {
    TempCorpus corpus;
    corpus.write("user/u.json",
                 R"({"signal": [{"target": "信号", "priority": 9}, {"target": "讯号", "priority": 3}]})");
    CorpusStore store(options_for(corpus));

    // Higher priority wins: _rank uses -priority, so 9 beats 3.
    REQUIRE(run(store, "signal").target_text == "信号");
}

TEST_CASE("a selected scene outranks a neutral entry in the same layer") {
    TempCorpus corpus;
    corpus.write("domain/d.json",
                 R"({"bank": [{"target": "岸"}, {"target": "河岸", "domain": "fiction"}]})");
    CorpusStore store(options_for(corpus));

    const Context fiction{"zh-CN", "fiction", ""};
    REQUIRE(run(store, "bank", "zh-CN", fiction).target_text == "河岸");
}

TEST_CASE("another scene prefers the neutral entry") {
    TempCorpus corpus;
    corpus.write("domain/d.json",
                 R"({"bank": [{"target": "岸"}, {"target": "河岸", "domain": "fiction"}]})");
    CorpusStore store(options_for(corpus));

    const Context finance{"zh-CN", "finance", ""};
    REQUIRE(run(store, "bank", "zh-CN", finance).target_text == "岸");
}

TEST_CASE("scene names compare case-insensitively") {
    TempCorpus corpus;
    corpus.write("domain/d.json",
                 R"({"bank": [{"target": "岸"}, {"target": "河岸", "domain": "fiction"}]})");
    CorpusStore store(options_for(corpus));

    const Context fiction{"zh-CN", "Fiction", ""};
    REQUIRE(run(store, "bank", "zh-CN", fiction).target_text == "河岸");
}

TEST_CASE("when_line selects between the two meanings of one word") {
    TempCorpus corpus;
    corpus.write("user/u.json", R"({"bank": [
        {"target": "银行", "when_line": "account|转账"},
        {"target": "河岸", "when_line": "river"}]})");
    CorpusStore store(options_for(corpus));

    REQUIRE(run(store, "bank account").target_text == "银行 account");
    REQUIRE(run(store, "river bank").target_text == "river 河岸");
}

TEST_CASE("a conditional entry does not apply when its line condition fails") {
    TempCorpus corpus;
    corpus.write("user/u.json", R"({"bank": {"target": "银行", "when_line": "account"}})");
    CorpusStore store(options_for(corpus));

    REQUIRE(run(store, "bank").target_text == "bank");
}

TEST_CASE("when_near sees words outside the matched term but not the term itself") {
    TempCorpus corpus;
    corpus.write("user/u.json", R"({"core": [
        {"target": "内丹", "when_near": "pill", "priority": 200},
        {"target": "核心", "priority": 100}]})");
    CorpusStore store(options_for(corpus));

    REQUIRE(run(store, "core pill").target_text == "内丹 pill");
    REQUIRE(run(store, "core").target_text == "核心");
}

TEST_CASE("when_window needs a known window that matches") {
    TempCorpus corpus;
    corpus.write("user/u.json", R"({"gate": {"target": "宗门大门", "when_window": "Watashi"}})");
    CorpusStore store(options_for(corpus));

    REQUIRE(run(store, "gate").target_text == "gate");
    const Context other{"zh-CN", "", "Other"};
    REQUIRE(run(store, "gate", "zh-CN", other).target_text == "gate");
    const Context watashi{"zh-CN", "", "Watashi Overlay"};
    REQUIRE(run(store, "gate", "zh-CN", watashi).target_text == "宗门大门");
}

TEST_CASE("two senses of one term both survive loading") {
    TempCorpus corpus;
    corpus.write("user/u.json", R"({"bank": [
        {"target": "银行", "when_line": "account"},
        {"target": "河岸", "when_line": "river"}]})");
    CorpusStore store(options_for(corpus));

    REQUIRE(store.size() == 2);
    REQUIRE(store.conflicts().empty());
}

TEST_CASE("a losing entry is reported as a conflict") {
    TempCorpus corpus;
    corpus.write("general/g.json", R"({"sword": "剑"})");
    corpus.write("user/u.json", R"({"sword": "本门之剑"})");
    CorpusStore store(options_for(corpus));

    REQUIRE(store.conflicts().size() == 1);
    REQUIRE(store.conflicts().front()["kept"]["target"] == "本门之剑");
}

TEST_CASE("a language-tagged entry only answers its own language") {
    TempCorpus corpus;
    corpus.write("general/g.json",
                 R"({"bank": {"target": "side", "lang": "en"}})");
    CorpusStore store(options_for(corpus));

    REQUIRE(run(store, "bank", "en").target_text == "side");
    REQUIRE(run(store, "bank", "zh-CN").target_text == "bank");
}

TEST_CASE("an untagged entry answers every target") {
    TempCorpus corpus;
    corpus.write("general/g.json", R"({"sword": "剑"})");
    CorpusStore store(options_for(corpus));

    REQUIRE(run(store, "sword", "zh-CN").target_text == "剑");
    REQUIRE(run(store, "sword", "ja").target_text == "剑");
}

TEST_CASE("CJK sources match through the language view") {
    TempCorpus corpus;
    corpus.write("general/g.json",
                 R"({"lang": "zh-CN", "entries": {"金丹": {"target": "golden core", "lang": "en"}}})");
    CorpusStore store(options_for(corpus));

    REQUIRE(run(store, "金丹", "en").target_text == "golden core");
    REQUIRE(run(store, "金丹", "zh-CN").target_text == "金丹");
}

TEST_CASE("a latin term does not match inside a longer word") {
    TempCorpus corpus;
    corpus.write("general/g.json", R"({"gg": "打得不错"})");
    CorpusStore store(options_for(corpus));

    REQUIRE(run(store, "dagger").target_text == "dagger");
    REQUIRE(run(store, "gg").target_text == "打得不错");
}

TEST_CASE("the longest corpus match wins") {
    TempCorpus corpus;
    corpus.write("general/g.json", R"({"gg": "不错", "gg wp": "打得好"})");
    CorpusStore store(options_for(corpus));

    REQUIRE(run(store, "gg wp").target_text == "打得好");
    REQUIRE(run(store, "gg noob").target_text == "不错 noob");
}

TEST_CASE("empty input is passed through with no spans") {
    TempCorpus corpus;
    corpus.write("general/g.json", R"({"sword": "剑"})");
    CorpusStore store(options_for(corpus));

    const Outcome outcome = run(store, "");
    REQUIRE(outcome.target_text.empty());
    REQUIRE(outcome.spans.empty());
    REQUIRE(outcome.coverage() == 0.0);
}

TEST_CASE("whitespace-only input is passed through unchanged") {
    TempCorpus corpus;
    corpus.write("general/g.json", R"({"sword": "剑"})");
    CorpusStore store(options_for(corpus));

    REQUIRE(run(store, "   ").target_text == "   ");
}

TEST_CASE("punctuation-only input stays literal and explains nothing") {
    TempCorpus corpus;
    corpus.write("general/g.json", R"({"sword": "剑"})");
    CorpusStore store(options_for(corpus));

    const Outcome outcome = run(store, "...");
    REQUIRE(outcome.target_text == "...");
    REQUIRE(outcome.coverage() == 0.0);
    REQUIRE(outcome.spans.size() == 1);
    REQUIRE(outcome.spans.front().origin == "literal");
}

TEST_CASE("coverage is explained characters over source characters") {
    TempCorpus corpus;
    corpus.write("general/g.json", R"({"sword": "剑"})");
    CorpusStore store(options_for(corpus));

    const Outcome outcome = run(store, "sword xyz");
    REQUIRE(outcome.target_text == "剑 xyz");
    REQUIRE(outcome.coverage() > 0.6);
    REQUIRE(outcome.coverage() < 0.7);
}

TEST_CASE("an edit on disk applies to the next lookup within 500 ms") {
    TempCorpus corpus;
    corpus.write("general/g.json", R"({"sword": "剑"})");
    CorpusStore store(options_for(corpus, true, 0.05));
    REQUIRE(run(store, "sword").target_text == "剑");

    corpus.write("general/g.json", R"({"sword": "剑意"})");
    const auto started = std::chrono::steady_clock::now();
    std::string translated;
    for (int attempt = 0; attempt < 10; ++attempt) {
        std::this_thread::sleep_for(std::chrono::milliseconds(60));
        translated = run(store, "sword").target_text;
        if (translated == "剑意") {
            break;
        }
    }
    const auto elapsed = std::chrono::steady_clock::now() - started;

    INFO("reload applied after " << std::chrono::duration_cast<std::chrono::milliseconds>(elapsed).count() << " ms");
    REQUIRE(translated == "剑意");
    REQUIRE(elapsed < std::chrono::milliseconds(500));
    REQUIRE(store.reloads() >= 1);
}

TEST_CASE("invalid JSON keeps the last good vocabulary and reports it") {
    TempCorpus corpus;
    corpus.write("general/g.json", R"({"sword": "剑"})");
    CorpusStore store(options_for(corpus, true, 0.05));
    REQUIRE(run(store, "sword").target_text == "剑");

    corpus.write("general/g.json", "{ this is not json");
    for (int attempt = 0; attempt < 10; ++attempt) {
        std::this_thread::sleep_for(std::chrono::milliseconds(60));
        if (run(store, "sword").target_text == "剑" && !store.last_error().empty()) {
            break;
        }
    }

    REQUIRE(run(store, "sword").target_text == "剑");
    REQUIRE_FALSE(store.last_error().empty());
}

TEST_CASE("the wrapped form declares a file language") {
    TempCorpus corpus;
    corpus.write("general/wrapped.json",
                 R"json({"lang": "en", "entries": {"bank": "bank(en)"}})json");
    CorpusStore store(options_for(corpus));

    REQUIRE(run(store, "bank", "en").target_text == "bank(en)");
    REQUIRE(run(store, "bank", "zh-CN").target_text == "bank");
    REQUIRE(store.languages() == std::vector<std::string>{"eng"});
}

TEST_CASE("a list value holds one answer per language") {
    TempCorpus corpus;
    corpus.write("general/g.json",
                 R"({"bank": [{"target": "岸", "lang": "zh-CN"}, {"target": "bank", "lang": "en"}]})");
    CorpusStore store(options_for(corpus));

    REQUIRE(run(store, "bank", "en").target_text == "bank");
    REQUIRE(run(store, "bank", "zh-CN").target_text == "岸");
}

TEST_CASE("the user layer can hide a shipped entry") {
    TempCorpus corpus;
    corpus.write("general/g.json", R"({"bank": "岸"})");
    corpus.write("user/u.json", R"({"_suppress": ["bank"]})");
    CorpusStore store(options_for(corpus));

    REQUIRE(run(store, "bank").target_text == "bank");
    REQUIRE(store.hidden_entry_count() == 1);
    REQUIRE(store.suppressed_sources().size() == 1);
}

TEST_CASE("revision advances when the vocabulary is replaced") {
    TempCorpus corpus;
    corpus.write("general/g.json", R"({"sword": "剑"})");
    CorpusStore store(options_for(corpus));
    const auto before = store.revision();

    store.load();

    REQUIRE(store.revision() > before);
}
