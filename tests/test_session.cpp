#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <cstddef>
#include <string>
#include <vector>

#include "watashi/events.hpp"
#include "watashi/pipeline.hpp"
#include "watashi/session.hpp"

namespace {

std::vector<watashi::Envelope> run_synthetic(double duration = 2.0, double fps = 10.0) {
    watashi::FakeOcr ocr;
    watashi::FakeTranslator translator;
    watashi::Session session({duration, fps}, ocr, translator);

    std::vector<watashi::Envelope> events;
    session.run([&events](const watashi::Envelope& envelope) {
        events.push_back(envelope);
    });
    return events;
}

/// A source that finds nothing, for the "empty input" case.
class EmptyOcr final : public watashi::OcrSource {
public:
    [[nodiscard]] watashi::OcrResult recognize(const watashi::ImageView&) const override {
        return {};
    }
};

}  // namespace

TEST_CASE("the first event is ready") {
    const auto events = run_synthetic();
    REQUIRE_FALSE(events.empty());
    REQUIRE(events.front().type == "ready");
}

TEST_CASE("the last event is stopped") {
    const auto events = run_synthetic();
    REQUIRE_FALSE(events.empty());
    REQUIRE(events.back().type == "stopped");
}

TEST_CASE("seq strictly increases across the stream") {
    const auto events = run_synthetic();
    REQUIRE(events.size() > 1);
    for (std::size_t index = 1; index < events.size(); ++index) {
        REQUIRE(events[index].seq == events[index - 1].seq + 1);
    }
    REQUIRE(events.front().seq == 1);
}

TEST_CASE("frame count matches duration times fps") {
    const auto events = run_synthetic(2.0, 10.0);
    const auto subtitles = std::count_if(
        events.begin(), events.end(),
        [](const watashi::Envelope& event) { return event.type == "subtitle"; });
    REQUIRE(subtitles == 20);
}

TEST_CASE("stats are emitted with numeric fields") {
    const auto events = run_synthetic();
    const auto stats = std::find_if(
        events.begin(), events.end(),
        [](const watashi::Envelope& event) { return event.type == "stats"; });
    REQUIRE(stats != events.end());
    REQUIRE(stats->data.at("frames").is_number_integer());
    REQUIRE(stats->data.at("ocr_ms").is_number());
    REQUIRE(stats->data.at("translate_ms").is_number());
    REQUIRE(stats->data.at("total_ms").is_number());
    REQUIRE(stats->data.at("mode") == "synthetic");
}

TEST_CASE("every subtitle carries lines with four-value boxes") {
    const auto events = run_synthetic(1.0, 10.0);
    int checked = 0;
    for (const auto& event : events) {
        if (event.type != "subtitle") {
            continue;
        }
        const auto& lines = event.data.at("lines");
        REQUIRE(lines.is_array());
        REQUIRE(lines.size() == 2);
        for (const auto& line : lines) {
            REQUIRE(line.at("source").is_string());
            REQUIRE(line.at("target").is_string());
            const auto& box = line.at("box");
            REQUIRE(box.is_array());
            REQUIRE(box.size() == 4);
            ++checked;
        }
    }
    REQUIRE(checked == 20);
}

TEST_CASE("ready carries the schema version") {
    const auto events = run_synthetic();
    REQUIRE(events.front().data.at("schema_version") == watashi::kSchemaVersion);
}

TEST_CASE("ready describes the synthetic region") {
    const auto events = run_synthetic();
    const auto& data = events.front().data;

    REQUIRE(data.at("region") == "synthetic 1200x200");
    const auto& box = data.at("region_box");
    REQUIRE(box.is_array());
    REQUIRE(box.size() == 4);
    REQUIRE(box.at(0) == 0);
    REQUIRE(box.at(1) == 0);
    REQUIRE(box.at(2) == 1200);
    REQUIRE(box.at(3) == 200);
}

TEST_CASE("stats carry paused and the measured split") {
    const auto events = run_synthetic();
    const auto stats = std::find_if(
        events.begin(), events.end(),
        [](const watashi::Envelope& event) { return event.type == "stats"; });
    REQUIRE(stats != events.end());

    REQUIRE(stats->data.at("paused") == false);
    REQUIRE(stats->data.at("ocr_ms").get<double>() >= 0.0);
    REQUIRE(stats->data.at("translate_ms").get<double>() >= 0.0);
    REQUIRE(stats->data.at("memory_mib").get<double>() >= 0.0);
}

TEST_CASE("a zero-length run still emits ready and stopped") {
    const auto events = run_synthetic(0.0, 10.0);

    REQUIRE(events.size() == 2);
    REQUIRE(events.front().type == "ready");
    REQUIRE(events.back().type == "stopped");
    REQUIRE(events.back().seq == 2);
}

TEST_CASE("an unknown command is an error") {
    watashi::FakeOcr ocr;
    watashi::FakeTranslator translator;
    watashi::Session session({1.0, 1.0}, ocr, translator);

    REQUIRE_THROWS_AS(session.handle_command("teleport"), watashi::ContractError);
}

TEST_CASE("an empty command is an error") {
    watashi::FakeOcr ocr;
    watashi::FakeTranslator translator;
    watashi::Session session({1.0, 1.0}, ocr, translator);

    REQUIRE_THROWS_AS(session.handle_command(""), watashi::ContractError);
}

TEST_CASE("pause and resume change the reported state") {
    watashi::FakeOcr ocr;
    watashi::FakeTranslator translator;
    watashi::Session session({1.0, 1.0}, ocr, translator);

    REQUIRE_FALSE(session.paused());
    (void)session.handle_command("pause");
    REQUIRE(session.paused());
    (void)session.handle_command("resume");
    REQUIRE_FALSE(session.paused());
}

TEST_CASE("toggle_pause flips the state back and forth") {
    watashi::FakeOcr ocr;
    watashi::FakeTranslator translator;
    watashi::Session session({1.0, 1.0}, ocr, translator);

    (void)session.handle_command("toggle_pause");
    REQUIRE(session.paused());
    (void)session.handle_command("toggle_pause");
    REQUIRE_FALSE(session.paused());
}

TEST_CASE("status answers with a command_result envelope") {
    watashi::FakeOcr ocr;
    watashi::FakeTranslator translator;
    watashi::Session session({1.0, 1.0}, ocr, translator);

    const auto result = session.handle_command("status");
    REQUIRE(result.type == "command_result");
    REQUIRE(result.data.at("cmd") == "status");
    REQUIRE(result.data.at("ok") == true);
    REQUIRE(result.seq == 1);
}

TEST_CASE("an OCR source that finds nothing does not crash") {
    EmptyOcr ocr;
    watashi::FakeTranslator translator;
    watashi::Session session({1.0, 1.0}, ocr, translator);

    std::vector<watashi::Envelope> events;
    session.run([&events](const watashi::Envelope& envelope) {
        events.push_back(envelope);
    });

    // ready, one subtitle, its stats, stopped.
    REQUIRE(events.size() == 4);
    REQUIRE(events.front().type == "ready");
    const auto subtitle = std::find_if(
        events.begin(), events.end(),
        [](const watashi::Envelope& event) { return event.type == "subtitle"; });
    REQUIRE(subtitle != events.end());
    REQUIRE(subtitle->data.at("lines").empty());
    REQUIRE(events.back().type == "stopped");
}
