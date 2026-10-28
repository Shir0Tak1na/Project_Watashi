#include "watashi/session.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <set>
#include <string>

namespace watashi {
namespace {

double steady_seconds() {
    return std::chrono::duration<double>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

constexpr std::string_view kKnownCommands[] = {
    "pause",
    "resume",
    "toggle_pause",
    "status",
    "shutdown",
};

}  // namespace

Session::Session(SessionOptions options, const OcrSource& ocr, const Translator& translator)
    : options_(options), pipeline_(ocr, translator) {}

void Session::run(const Sink& sink) {
    const double duration = std::max(0.0, options_.duration_s);
    const double fps = std::max(0.0, options_.fps);
    const int frame_count = static_cast<int>(std::lround(duration * fps));

    emit(sink, event_type::kReady,
         nlohmann::json{
             {"schema_version", kSchemaVersion},
             {"mode", "synthetic"},
             {"fps", fps},
             {"duration_s", duration},
             {"backend", "fake"},
         });

    frames_ = 0;
    for (int index = 0; index < frame_count; ++index) {
        const double started = steady_seconds();
        const OverlayUpdate update = pipeline_.process(index, started);
        emit(sink, event_type::kSubtitle, update.to_json());
        ++frames_;

        emit(sink, event_type::kStats,
             nlohmann::json{
                 {"fps", fps},
                 {"ocr_ms", 0.0},
                 {"translate_ms", update.latency_ms},
                 {"total_ms", update.latency_ms},
                 {"frames", frames_},
                 {"skipped", 0},
                 {"corpus_entries", 0},
                 {"rules", 0},
                 {"cache_hit_rate", 0.0},
                 {"refinements", 0},
                 {"refinements_pending", 0},
                 {"refinements_dropped", 0},
                 {"memory_mib", 0.0},
                 {"mode", "synthetic"},
                 {"backend", "fake"},
             });
    }

    emit(sink, event_type::kStopped,
         nlohmann::json{{"reason", "duration"}, {"frames", frames_}});
}

Envelope Session::handle_command(std::string_view command) {
    const auto known = std::find(
        std::begin(kKnownCommands), std::end(kKnownCommands), command);
    if (command.empty() || known == std::end(kKnownCommands)) {
        throw ContractError("unknown command \"" + std::string(command) + "\"");
    }

    std::string detail;
    if (command == "pause") {
        paused_ = true;
        detail = "paused";
    } else if (command == "resume") {
        paused_ = false;
        detail = "running";
    } else if (command == "toggle_pause") {
        paused_ = !paused_;
        detail = paused_ ? "paused" : "running";
    } else if (command == "status") {
        detail = paused_ ? "paused" : "running";
    } else {
        detail = "shutdown requested";
    }

    return encode_event(
        event_type::kCommandResult,
        nlohmann::json{
            {"cmd", std::string(command)},
            {"ok", true},
            {"detail", detail},
        },
        ++seq_,
        now_seconds());
}

Envelope Session::emit(const Sink& sink, std::string_view type, nlohmann::json data) {
    Envelope envelope = encode_event(type, std::move(data), ++seq_, now_seconds());
    if (sink) {
        sink(envelope);
    }
    return envelope;
}

}  // namespace watashi
