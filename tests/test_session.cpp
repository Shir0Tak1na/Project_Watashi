#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <cstddef>
#include <memory>
#include <string>
#include <string_view>
#include <utility>
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

/// Not the deterministic FakeOcr, so the pipeline treats it as the real ONNX
/// path. It stands in for a loaded PP-OCRv4 model without needing the weights.
class StubOcr final : public watashi::OcrSource {
public:
    [[nodiscard]] watashi::OcrResult recognize(const watashi::ImageView&) const override {
        watashi::OcrResult result;
        watashi::OcrLine line;
        line.text = "hello";
        line.box = watashi::Box{1, 2, 30, 10};
        result.lines.push_back(line);
        return result;
    }
};

/// Names a model-backed layer, so the frame has a real translator backend to
/// aggregate rather than the skeleton's "fake".
class StubNmtTranslator final : public watashi::Translator {
public:
    [[nodiscard]] watashi::Translation translate(
        std::string_view source,
        const watashi::TranslateContext&) const override {
        (void)source;
        watashi::Translation result;
        result.target = "你好";
        result.backend = "local-nmt/int8";
        result.trace = "local-nmt/int8";
        result.used_model = true;
        return result;
    }
};

/// A translator whose provenance is chosen by the test, so the four engine
/// paths can be compared without a corpus file or a model.
class StubTranslator final : public watashi::Translator {
public:
    StubTranslator(std::string backend, std::string trace)
        : backend_(std::move(backend)), trace_(std::move(trace)) {}

    [[nodiscard]] watashi::Translation translate(
        std::string_view source,
        const watashi::TranslateContext&) const override {
        (void)source;
        watashi::Translation result;
        result.target = "你好";
        result.backend = backend_;
        result.trace = trace_;
        return result;
    }

private:
    std::string backend_;
    std::string trace_;
};

struct PathResult {
    std::string backend;
    std::string trace;
};

PathResult first_subtitle_provenance(std::unique_ptr<watashi::OcrSource> ocr,
                                     std::unique_ptr<watashi::Translator> translator) {
    watashi::Session session({1.0, 1.0}, *ocr, *translator);
    std::vector<watashi::Envelope> events;
    session.run([&events](const watashi::Envelope& envelope) {
        events.push_back(envelope);
    });
    const auto subtitle = std::find_if(
        events.begin(), events.end(),
        [](const watashi::Envelope& event) { return event.type == "subtitle"; });
    REQUIRE(subtitle != events.end());
    return PathResult{subtitle->data.at("backend").get<std::string>(),
                      subtitle->data.at("trace").get<std::string>()};
}

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

TEST_CASE("the skeleton path keeps the historical fake backend and trace") {
    const auto events = run_synthetic(1.0, 10.0);
    const auto subtitle = std::find_if(
        events.begin(), events.end(),
        [](const watashi::Envelope& event) { return event.type == "subtitle"; });
    REQUIRE(subtitle != events.end());

    REQUIRE(subtitle->data.at("backend") == "fake");
    REQUIRE(subtitle->data.at("trace") ==
            "fake: skeleton path, no OCR and no model");
}

TEST_CASE("a real OCR and translator report their own backends") {
    StubOcr ocr;
    StubNmtTranslator translator;
    watashi::Session session({1.0, 1.0}, ocr, translator);

    std::vector<watashi::Envelope> events;
    session.run([&events](const watashi::Envelope& envelope) {
        events.push_back(envelope);
    });

    const auto subtitle = std::find_if(
        events.begin(), events.end(),
        [](const watashi::Envelope& event) { return event.type == "subtitle"; });
    REQUIRE(subtitle != events.end());

    const auto backend = subtitle->data.at("backend").get<std::string>();
    const auto trace = subtitle->data.at("trace").get<std::string>();
    REQUIRE(backend != "fake");
    REQUIRE(backend.find("onnx") != std::string::npos);
    REQUIRE(backend.find("local-nmt/int8") != std::string::npos);
    REQUIRE(trace.rfind("ocr: onnx", 0) == 0);
    REQUIRE(trace.find("local-nmt/int8") != std::string::npos);
}

TEST_CASE("an empty real frame still names the OCR layer") {
    EmptyOcr ocr;
    StubNmtTranslator translator;
    watashi::Session session({1.0, 1.0}, ocr, translator);

    std::vector<watashi::Envelope> events;
    session.run([&events](const watashi::Envelope& envelope) {
        events.push_back(envelope);
    });

    const auto subtitle = std::find_if(
        events.begin(), events.end(),
        [](const watashi::Envelope& event) { return event.type == "subtitle"; });
    REQUIRE(subtitle != events.end());
    REQUIRE(subtitle->data.at("backend") == "onnx");
    REQUIRE(subtitle->data.at("trace") == "ocr: onnx");
}

TEST_CASE("backend and trace are composed the same way on all four engine paths") {
    // One table pins the contract: ``backend`` names the layers that answered,
    // ``trace`` starts with the OCR layer and then lists each distinct
    // translator trace once. The fake skeleton keeps its historical values.
    const std::string corpus_trace = "\"hello\"->\"你好\"[corpus 0.90]";

    const auto fake_fake = first_subtitle_provenance(
        std::make_unique<watashi::FakeOcr>(),
        std::make_unique<watashi::FakeTranslator>());
    REQUIRE(fake_fake.backend == "fake");
    REQUIRE(fake_fake.trace == "fake: skeleton path, no OCR and no model");

    const auto fake_corpus = first_subtitle_provenance(
        std::make_unique<watashi::FakeOcr>(),
        std::make_unique<StubTranslator>("corpus+rules", corpus_trace));
    REQUIRE(fake_corpus.backend == "fake, corpus+rules");
    REQUIRE(fake_corpus.trace == "ocr: fake\n" + corpus_trace);

    const auto onnx_corpus = first_subtitle_provenance(
        std::make_unique<StubOcr>(),
        std::make_unique<StubTranslator>("corpus+rules", corpus_trace));
    REQUIRE(onnx_corpus.backend == "onnx, corpus+rules");
    REQUIRE(onnx_corpus.trace == "ocr: onnx\n" + corpus_trace);

    const auto onnx_nmt = first_subtitle_provenance(
        std::make_unique<StubOcr>(),
        std::make_unique<StubTranslator>("local-nmt/int8", "local-nmt/int8"));
    REQUIRE(onnx_nmt.backend == "onnx, local-nmt/int8");
    REQUIRE(onnx_nmt.trace == "ocr: onnx\nlocal-nmt/int8");
}
