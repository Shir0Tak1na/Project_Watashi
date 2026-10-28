#include "watashi/session.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <set>
#include <string>
#include <vector>

#if defined(_WIN32)
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <psapi.h>
#endif

namespace watashi {
namespace {

double steady_seconds() {
    return std::chrono::duration<double>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

/// Resident memory, in MiB, as the stats contract reports it. 0.0 when the
/// platform cannot answer, which is honest: the field is present because the
/// payload is fixed, not because this build measured something it did not.
double working_set_mib() {
#if defined(_WIN32)
    PROCESS_MEMORY_COUNTERS counters{};
    if (GetProcessMemoryInfo(GetCurrentProcess(), &counters, sizeof(counters))) {
        return static_cast<double>(counters.WorkingSetSize) / (1024.0 * 1024.0);
    }
#endif
    return 0.0;
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
             {"region", "synthetic " + std::to_string(options_.region_width) + "x" +
                            std::to_string(options_.region_height)},
             {"region_box", nlohmann::json::array(
                                {0, 0, options_.region_width, options_.region_height})},
             {"mode", "synthetic"},
             {"fps", fps},
             {"duration_s", duration},
             {"backend", "fake"},
         });

    frames_ = 0;
    // The fake source ignores the pixels, but the interface is the real one, so
    // the session hands it a real buffer of the region's size: a 1200x200x3
    // blank frame costs 720 KiB once and keeps the seam honest.
    const std::size_t frame_bytes = static_cast<std::size_t>(std::max(1, options_.region_width)) *
                                    static_cast<std::size_t>(std::max(1, options_.region_height)) * 3;
    const std::vector<std::uint8_t> frame(frame_bytes, 0);
    const ImageView view{frame.data(), std::max(1, options_.region_width),
                         std::max(1, options_.region_height)};

    for (int index = 0; index < frame_count; ++index) {
        const double started = steady_seconds();
        const OverlayUpdate update = pipeline_.process(view, started);
        emit(sink, event_type::kSubtitle, update.to_json());
        ++frames_;

        emit(sink, event_type::kStats,
             nlohmann::json{
                 {"fps", fps},
                 {"ocr_ms", update.ocr_ms},
                 {"translate_ms", update.translate_ms},
                 {"total_ms", update.latency_ms},
                 {"frames", frames_},
                 {"skipped", 0},
                 {"corpus_entries", 0},
                 {"rules", 0},
                 {"cache_hit_rate", 0.0},
                 {"backend", "fake"},
                 {"paused", paused_},
                 {"refinements", 0},
                 {"refinements_pending", 0},
                 {"refinements_dropped", 0},
                 {"memory_mib", working_set_mib()},
                 {"mode", "synthetic"},
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
