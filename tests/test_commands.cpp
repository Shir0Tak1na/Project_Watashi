// The stdin command channel: parsing, payloads, pause, shutdown and the
// single-writer sequence guarantee when a reader thread posts commands.

#include <catch2/catch_approx.hpp>
#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

#include "watashi/capture.hpp"
#include "watashi/commands.hpp"
#include "watashi/events.hpp"
#include "watashi/pipeline.hpp"
#include "watashi/session.hpp"

namespace {

using namespace watashi;

bool wait_until(const std::function<bool()>& predicate, int timeout_ms) {
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::milliseconds(timeout_ms);
    while (std::chrono::steady_clock::now() < deadline) {
        if (predicate()) {
            return true;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    return predicate();
}

class ContextTranslator final : public Translator {
public:
    [[nodiscard]] Translation translate(
        std::string_view source,
        const TranslateContext& context) const override {
        Translation result;
        result.target = context.target_lang + "|" + std::string(source);
        result.backend = "context-test";
        return result;
    }
};

class Collector {
public:
    void push(const Envelope& envelope) {
        std::lock_guard<std::mutex> lock(mutex_);
        events_.push_back(envelope);
    }

    [[nodiscard]] std::vector<Envelope> events() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return events_;
    }

    [[nodiscard]] std::size_t count(const std::string& type) const {
        std::lock_guard<std::mutex> lock(mutex_);
        std::size_t total = 0;
        for (const auto& event : events_) {
            if (event.type == type) {
                ++total;
            }
        }
        return total;
    }

private:
    mutable std::mutex mutex_;
    std::vector<Envelope> events_;
};

}  // namespace

TEST_CASE("a command line parses into its document", "[commands]") {
    const CommandMessage message =
        parse_command_line(R"({"cmd":"pause"})");
    REQUIRE(message.valid());
    REQUIRE(message.document.at("cmd") == "pause");
}

TEST_CASE("bad JSON becomes an error message, not an exception", "[commands]") {
    const CommandMessage message = parse_command_line("{not json");
    REQUIRE_FALSE(message.valid());
    REQUIRE(message.error.find("bad JSON") != std::string::npos);
}

TEST_CASE("an empty or non-object line is rejected", "[commands]") {
    REQUIRE_FALSE(parse_command_line("   ").valid());
    REQUIRE_FALSE(parse_command_line("[1,2]").valid());
    REQUIRE(parse_command_line("[1,2]").error.find("object") != std::string::npos);
}

TEST_CASE("the command queue preserves order and size", "[commands]") {
    CommandQueue queue;
    REQUIRE(queue.size() == 0);
    queue.push(parse_command_line(R"({"cmd":"pause"})"));
    queue.push(parse_command_line("{bad"));
    REQUIRE(queue.size() == 2);

    CommandMessage first;
    REQUIRE(queue.try_pop(first));
    REQUIRE(first.valid());
    REQUIRE(first.document.at("cmd") == "pause");

    CommandMessage second;
    REQUIRE(queue.try_pop(second));
    REQUIRE_FALSE(second.valid());
    REQUIRE_FALSE(queue.try_pop(first));
}

TEST_CASE("payload commands update the session", "[commands]") {
    FakeOcr ocr;
    FakeTranslator translator;
    Session session({1.0, 1.0}, ocr, translator);

    const auto pause = session.handle_command_json(
        nlohmann::json{{"cmd", "pause"}});
    REQUIRE(pause.data.at("ok") == true);
    REQUIRE(session.paused());

    const auto resume = session.handle_command_json(
        nlohmann::json{{"cmd", "resume"}});
    REQUIRE(resume.data.at("ok") == true);
    REQUIRE_FALSE(session.paused());

    const auto target = session.handle_command_json(nlohmann::json{
        {"cmd", "set_target_lang"}, {"target_lang", "en"}});
    REQUIRE(target.data.at("ok") == true);
    REQUIRE(session.context().target_lang == "en");

    const auto scene = session.handle_command_json(
        nlohmann::json{{"cmd", "set_scene"}, {"scene", "cultivation"}});
    REQUIRE(scene.data.at("ok") == true);
    REQUIRE(session.context().scene == "cultivation");

    const auto fps = session.handle_command_json(
        nlohmann::json{{"cmd", "set_fps"}, {"value", 25.0}});
    REQUIRE(fps.data.at("ok") == true);
    REQUIRE(session.fps() == Catch::Approx(25.0));

    const auto threshold = session.handle_command_json(
        nlohmann::json{{"cmd", "set_diff_threshold"}, {"value", 0.4}});
    REQUIRE(threshold.data.at("ok") == true);
    REQUIRE(session.diff_threshold() == Catch::Approx(0.4));
}

TEST_CASE("bad payloads answer ok=false", "[commands]") {
    FakeOcr ocr;
    FakeTranslator translator;
    Session session({1.0, 1.0}, ocr, translator);

    const auto unknown = session.handle_command_json(
        nlohmann::json{{"cmd", "teleport"}});
    REQUIRE(unknown.data.at("ok") == false);
    REQUIRE(unknown.data.at("cmd") == "teleport");

    const auto no_cmd = session.handle_command_json(nlohmann::json{{"x", 1}});
    REQUIRE(no_cmd.data.at("ok") == false);
    REQUIRE(no_cmd.data.at("cmd") == "");

    const auto lang = session.handle_command_json(
        nlohmann::json{{"cmd", "set_target_lang"}});
    REQUIRE(lang.data.at("ok") == false);

    const auto fps = session.handle_command_json(
        nlohmann::json{{"cmd", "set_fps"}, {"value", 0.0}});
    REQUIRE(fps.data.at("ok") == false);

    const auto ratio = session.handle_command_json(
        nlohmann::json{{"cmd", "set_diff_threshold"}, {"value", 2.0}});
    REQUIRE(ratio.data.at("ok") == false);
}

TEST_CASE("reload_corpus uses the run's callback", "[commands]") {
    FakeOcr ocr;
    FakeTranslator translator;
    SessionOptions options;
    options.reload_corpus = []() { return std::string("corpus: 42 entries"); };
    Session session(options, ocr, translator);

    const auto result = session.handle_command_json(
        nlohmann::json{{"cmd", "reload_corpus"}});
    REQUIRE(result.data.at("ok") == true);
    REQUIRE(result.data.at("detail") == "corpus: 42 entries");
}

TEST_CASE("reload_corpus without a corpus answers ok=false", "[commands]") {
    FakeOcr ocr;
    FakeTranslator translator;
    Session session({1.0, 1.0}, ocr, translator);
    const auto result = session.handle_command_json(
        nlohmann::json{{"cmd", "reload_corpus"}});
    REQUIRE(result.data.at("ok") == false);
}

TEST_CASE("shutdown is recorded and answered", "[commands]") {
    FakeOcr ocr;
    FakeTranslator translator;
    Session session({1.0, 1.0}, ocr, translator);
    const auto result = session.handle_command_json(
        nlohmann::json{{"cmd", "shutdown"}});
    REQUIRE(result.data.at("ok") == true);
    REQUIRE(session.shutdown_requested());
}

TEST_CASE("a queued shutdown ends the stream with reason shutdown", "[commands]") {
    SyntheticCapture capture({});
    FakeOcr ocr;
    FakeTranslator translator;
    SessionOptions options;
    options.duration_s = 5.0;
    options.fps = 10.0;
    options.capture = &capture;
    Session session(options, ocr, translator);

    Collector collector;
    std::thread runner([&]() {
        session.run([&collector](const Envelope& envelope) {
            collector.push(envelope);
        });
    });

    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    session.post_command(nlohmann::json{{"cmd", "status"}});
    session.post_command(nlohmann::json{{"cmd", "shutdown"}});
    runner.join();

    const auto events = collector.events();
    REQUIRE_FALSE(events.empty());
    REQUIRE(events.front().type == "ready");
    REQUIRE(events.back().type == "stopped");
    REQUIRE(events.back().data.at("reason") == "shutdown");
    REQUIRE(collector.count("command_result") == 2);
    for (std::size_t index = 1; index < events.size(); ++index) {
        REQUIRE(events[index].seq == events[index - 1].seq + 1);
    }
}

TEST_CASE("pause stops subtitles and resume starts them again", "[commands]") {
    SyntheticCapture capture({});
    FakeOcr ocr;
    FakeTranslator translator;
    SessionOptions options;
    options.duration_s = 4.0;
    options.fps = 10.0;
    options.capture = &capture;
    Session session(options, ocr, translator);

    Collector collector;
    std::thread runner([&]() {
        session.run([&collector](const Envelope& envelope) {
            collector.push(envelope);
        });
    });

    REQUIRE(wait_until([&] { return collector.count("subtitle") >= 2; }, 2000));
    session.post_command(nlohmann::json{{"cmd", "pause"}});
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
    const std::size_t paused_count = collector.count("subtitle");
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
    REQUIRE(collector.count("subtitle") == paused_count);

    session.post_command(nlohmann::json{{"cmd", "resume"}});
    REQUIRE(wait_until(
        [&] { return collector.count("subtitle") > paused_count; }, 2000));
    session.post_command(nlohmann::json{{"cmd", "shutdown"}});
    runner.join();
    REQUIRE(collector.count("command_result") >= 3);
}

TEST_CASE("a malformed queued message is answered with ok=false", "[commands]") {
    SyntheticCapture capture({});
    FakeOcr ocr;
    FakeTranslator translator;
    SessionOptions options;
    options.duration_s = 3.0;
    options.fps = 10.0;
    options.capture = &capture;
    Session session(options, ocr, translator);

    Collector collector;
    std::thread runner([&]() {
        session.run([&collector](const Envelope& envelope) {
            collector.push(envelope);
        });
    });

    CommandMessage message;
    message.error = "bad JSON: test";
    session.post_command(message);
    session.post_command(nlohmann::json{{"cmd", "shutdown"}});
    runner.join();

    const auto events = collector.events();
    const auto result = std::find_if(
        events.begin(), events.end(),
        [](const Envelope& event) { return event.type == "command_result"; });
    REQUIRE(result != events.end());
    REQUIRE(result->data.at("ok") == false);
    REQUIRE(result->data.at("cmd") == "");
}

TEST_CASE("a target language change reprocesses an unchanged screen", "[commands]") {
    // The screen never changes, so the still-screen skip would keep serving the
    // subtitle translated for the old language. The context change must reset
    // the detector and translate the next frame again.
    SyntheticCapture capture({});
    FakeOcr ocr;
    ContextTranslator translator;
    SessionOptions options;
    options.duration_s = 3.0;
    options.fps = 10.0;
    options.capture = &capture;
    options.context = TranslateContext{"zh-CN", "", ""};
    Session session(options, ocr, translator);

    Collector collector;
    std::thread runner([&]() {
        session.run([&collector](const Envelope& envelope) {
            collector.push(envelope);
        });
    });

    REQUIRE(wait_until([&] { return collector.count("subtitle") >= 1; }, 2000));
    session.post_command(nlohmann::json{
        {"cmd", "set_target_lang"}, {"target_lang", "en"}});
    const bool saw_en = wait_until(
        [&] {
            const auto events = collector.events();
            return std::any_of(
                events.begin(), events.end(), [](const Envelope& event) {
                    return event.type == "subtitle" &&
                           event.data.value("target", std::string{}).rfind("en|", 0) == 0;
                });
        },
        2000);
    REQUIRE(saw_en);

    session.post_command(nlohmann::json{{"cmd", "shutdown"}});
    runner.join();
}

TEST_CASE("many threads posting commands keep seq single-writer", "[commands]") {
    SyntheticCapture capture({});
    FakeOcr ocr;
    FakeTranslator translator;
    SessionOptions options;
    options.duration_s = 3.0;
    options.fps = 10.0;
    options.capture = &capture;
    Session session(options, ocr, translator);

    Collector collector;
    std::thread runner([&]() {
        session.run([&collector](const Envelope& envelope) {
            collector.push(envelope);
        });
    });

    std::vector<std::thread> posters;
    for (int thread = 0; thread < 4; ++thread) {
        posters.emplace_back([&session, thread]() {
            for (int index = 0; index < 5; ++index) {
                session.post_command(nlohmann::json{
                    {"cmd", "status"},
                    {"thread", thread},
                    {"index", index},
                });
            }
        });
    }
    for (auto& poster : posters) {
        poster.join();
    }
    session.post_command(nlohmann::json{{"cmd", "shutdown"}});
    runner.join();

    const auto events = collector.events();
    REQUIRE(collector.count("command_result") == 21);
    for (std::size_t index = 1; index < events.size(); ++index) {
        REQUIRE(events[index].seq == events[index - 1].seq + 1);
    }
}
