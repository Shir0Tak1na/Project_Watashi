#include "watashi/session.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <memory>
#include <set>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#if defined(_WIN32)
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <psapi.h>
#include <timeapi.h>
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

/// The default Windows timer resolution is ~15.6 ms, so a 10 fps pacing request
/// would drift into ~13 fps. A live run raises it to 1 ms and restores it on
/// scope exit, which keeps both the frame rate and the duration honest.
class TimerResolution {
public:
    TimerResolution() {
#if defined(_WIN32)
        if (timeBeginPeriod(1) == TIMERR_NOERROR) {
            raised_ = true;
        }
#endif
    }
    ~TimerResolution() {
#if defined(_WIN32)
        if (raised_) {
            timeEndPeriod(1);
        }
#endif
    }
    TimerResolution(const TimerResolution&) = delete;
    TimerResolution& operator=(const TimerResolution&) = delete;

private:
    bool raised_ = false;
};

struct Distribution {
    double p50 = 0.0;
    double p95 = 0.0;
    double min = 0.0;
    double max = 0.0;

    [[nodiscard]] nlohmann::json to_json() const {
        return nlohmann::json{
            {"p50", p50},
            {"p95", p95},
            {"min", min},
            {"max", max},
            {"count", static_cast<int>(count)},
        };
    }

    int count = 0;
};

Distribution summarise(std::vector<double> values) {
    Distribution result;
    result.count = static_cast<int>(values.size());
    if (values.empty()) {
        return result;
    }
    std::sort(values.begin(), values.end());
    const auto at = [&values](double quantile) {
        const double raw = std::ceil(quantile * static_cast<double>(values.size())) - 1.0;
        const std::size_t index = static_cast<std::size_t>(
            std::min(std::max(0.0, raw), static_cast<double>(values.size() - 1)));
        return values[index];
    };
    result.p50 = at(0.50);
    result.p95 = at(0.95);
    result.min = values.front();
    result.max = values.back();
    return result;
}

void write_bench(const std::string& path,
                 const nlohmann::json& document) {
    std::ofstream file(path, std::ios::binary | std::ios::trunc);
    if (!file) {
        throw std::runtime_error("cannot write bench file: " + path);
    }
    file << document.dump(2) << '\n';
}

}  // namespace

Session::Session(SessionOptions options, const OcrSource& ocr, const Translator& translator)
    : options_(std::move(options)),
      pipeline_(ocr, translator, options_.context) {
    fps_ = std::max(0.0, options_.fps);
    diff_threshold_ = options_.diff.threshold;
    reload_corpus_ = options_.reload_corpus;
}

void Session::run(const Sink& sink) {
    const double duration = std::max(0.0, options_.duration_s);
    const double fps = fps_ > 0.0 ? fps_ : std::max(0.0, options_.fps);
    const int frame_count = static_cast<int>(std::lround(duration * fps));

    const bool captured = options_.capture != nullptr;
    CaptureInfo capture_info;
    if (captured) {
        capture_info = options_.capture->info();
    }

    // Default to the synthetic shape the contract already publishes; a capture
    // source overrides it with its own rectangle and description.
    const std::string mode = captured && !capture_info.mode.empty() ? capture_info.mode
                                                                    : std::string("synthetic");
    CaptureRect region = captured ? capture_info.region
                                  : CaptureRect{0, 0, std::max(1, options_.region_width),
                                                std::max(1, options_.region_height)};
    std::string region_label;
    if (captured && !capture_info.description.empty()) {
        region_label = capture_info.description;
    } else if (captured) {
        region_label = mode + " " + std::to_string(region.width) + "x" +
                       std::to_string(region.height) + "+" + std::to_string(region.x) + "+" +
                       std::to_string(region.y);
    } else {
        region_label = "synthetic " + std::to_string(region.width) + "x" +
                       std::to_string(region.height);
    }

    nlohmann::json ready{
        {"schema_version", kSchemaVersion},
        {"region", region_label},
        {"region_box", nlohmann::json::array(
                           {region.x, region.y, region.right(), region.bottom()})},
        {"mode", mode},
        {"fps", fps},
        {"duration_s", duration},
        {"backend", options_.backend_label},
    };
    if (captured) {
        ready["capture_mode"] = mode;
        ready["diff_threshold"] = options_.diff.threshold;
    }
    emit(sink, event_type::kReady, std::move(ready));

    const int width = std::max(1, region.width);
    const int height = std::max(1, region.height);
    // The synthetic path ignores the pixels, but the interface is the real one,
    // so it hands over a real buffer of the region's size: a 1200x200x3 blank
    // frame costs 720 KiB once and keeps the seam honest.
    std::vector<std::uint8_t> placeholder;
    if (!captured) {
        placeholder.assign(
            static_cast<std::size_t>(width) * static_cast<std::size_t>(height) * 3, 0);
    }
    const ImageView placeholder_view{placeholder.empty() ? nullptr : placeholder.data(),
                                     placeholder.empty() ? 0 : width,
                                     placeholder.empty() ? 0 : height};

    ChangeDetector detector(options_.diff);
    detector.set_threshold(diff_threshold_);
    const bool pacing = captured;

    OverlayUpdate last_update;
    bool have_update = false;
    int skipped = 0;
    int ocr_calls = 0;
    std::vector<double> capture_ms;
    std::vector<double> e2e_ms;
    std::vector<double> ocr_ms;
    double last_difference = 0.0;

    const double run_started = steady_seconds();
    std::unique_ptr<TimerResolution> timer;
    if (pacing) {
        timer = std::make_unique<TimerResolution>();
    }

    frames_ = 0;
    int frame_index = 0;
    while (frame_index < frame_count && !shutdown_.load()) {
        // Commands are applied between frames, on this thread, so the event
        // stream and ``seq`` stay single-writer and a stdin reader never races
        // with OCR or translation.
        drain_commands(sink, &detector);
        if (shutdown_.load()) {
            break;
        }
        if (reprocess_) {
            // set_target_lang/set_scene invalidated the cached subtitle.
            detector.reset();
            reprocess_ = false;
        }
        if (paused_.load()) {
            // The engine pauses: no capture, no OCR, no subtitle and no stats.
            // Keep draining commands so resume/shutdown are seen immediately.
            if (duration > 0.0 && steady_seconds() - run_started >= duration) {
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
            continue;
        }

        ImageView view = placeholder_view;
        double started = steady_seconds();
        double capture_cost = 0.0;
        bool unchanged = false;
        // The frame has to outlive the pipeline call: ``view`` borrows its
        // bytes, so a frame declared inside the branch would dangle by the time
        // OCR reads it.
        CaptureFrame frame;

        if (captured) {
            frame = options_.capture->grab();
            view = frame.view();
            if (view.empty()) {
                throw CaptureError("capture source returned no pixels");
            }
            started = frame.t_start_s;
            capture_cost = (frame.t_end_s - frame.t_start_s) * 1000.0;
            const bool use_diff = captured && detector.enabled();
            if (use_diff) {
                unchanged = !detector.changed(view);
                last_difference = detector.last_difference();
            }
        }

        OverlayUpdate update;
        if (unchanged && have_update) {
            // The screen did not change, so the subtitle did not either. Reuse
            // the last payload rather than paying for OCR that would read the
            // same pixels again.
            ++skipped;
            update = last_update;
            update.latency_ms = (steady_seconds() - started) * 1000.0;
            update.ocr_ms = 0.0;
            update.translate_ms = 0.0;
        } else {
            update = pipeline_.process(view, started);
            ++ocr_calls;
            if (captured) {
                last_update = update;
                have_update = true;
            }
        }

        emit(sink, event_type::kSubtitle, update.to_json());
        ++frames_;
        ++frame_index;

        capture_ms.push_back(capture_cost);
        e2e_ms.push_back(update.latency_ms);
        if (!unchanged) {
            ocr_ms.push_back(update.ocr_ms);
        }

        nlohmann::json stats{
            {"fps", fps_},
            {"ocr_ms", update.ocr_ms},
            {"translate_ms", update.translate_ms},
            {"total_ms", update.latency_ms},
            {"frames", frames_},
            {"skipped", skipped},
            {"capture_ms", capture_cost},
            {"ocr_calls", ocr_calls},
            {"diff", last_difference},
            {"corpus_entries", 0},
            {"rules", 0},
            {"cache_hit_rate", 0.0},
            {"backend", options_.backend_label},
            {"paused", paused_.load()},
            {"refinements", 0},
            {"refinements_pending", 0},
            {"refinements_dropped", 0},
            {"memory_mib", working_set_mib()},
            {"mode", mode},
        };
        emit(sink, event_type::kStats, std::move(stats));

        // Pace a real capture at the requested rate. The deadline is absolute,
        // as before, so per-frame overhead cannot stretch the run; the sleep is
        // sliced so a command is answered within a few tens of milliseconds even
        // at 1 fps.
        if (pacing) {
            const double period = fps_ > 0.0 ? 1.0 / fps_ : 0.0;
            const double deadline =
                run_started + static_cast<double>(frame_index) * period;
            while (!shutdown_.load()) {
                const double remaining = deadline - steady_seconds();
                if (remaining <= 0.0) {
                    break;
                }
                std::this_thread::sleep_for(
                    std::chrono::duration<double>(std::min(remaining, 0.02)));
                drain_commands(sink, &detector);
            }
        }

        // A grab slower than a frame period must not stretch "5 s" into a
        // longer run: stop once the wall clock has had its time.
        if (captured && duration > 0.0 && steady_seconds() - run_started >= duration) {
            break;
        }
    }

    emit(sink, event_type::kStopped,
         nlohmann::json{{"reason", shutdown_.load() ? "shutdown" : "duration"},
                        {"frames", frames_}});

    if (!options_.bench_path.empty()) {
        write_bench(
            options_.bench_path,
            nlohmann::json{
                {"kind", "capture-bench"},
                {"mode", mode},
                {"region", nlohmann::json::array(
                               {region.x, region.y, region.width, region.height})},
                {"fps", fps_},
                {"duration_s", duration},
                {"frames", frames_},
                {"ocr_calls", ocr_calls},
                {"skipped", skipped},
                {"diff_threshold", diff_threshold_},
                {"capture_ms", summarise(capture_ms).to_json()},
                {"e2e_ms", summarise(e2e_ms).to_json()},
                {"ocr_ms", summarise(ocr_ms).to_json()},
                {"backend", options_.backend_label},
            });
    }
}

Envelope Session::handle_command(std::string_view command) {
    const auto known = std::find(
        std::begin(kKnownCommands), std::end(kKnownCommands), command);
    if (command.empty() || known == std::end(kKnownCommands)) {
        throw ContractError("unknown command \"" + std::string(command) + "\"");
    }

    std::string detail;
    if (command == "pause") {
        paused_.store(true);
        detail = "paused";
    } else if (command == "resume") {
        paused_.store(false);
        detail = "running";
    } else if (command == "toggle_pause") {
        const bool now_paused = !paused_.load();
        paused_.store(now_paused);
        detail = now_paused ? "paused" : "running";
    } else if (command == "status") {
        detail = paused_.load() ? "paused" : "running";
    } else {
        shutdown_.store(true);
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

Envelope Session::handle_command_json(const nlohmann::json& command) {
    return encode_event(
        event_type::kCommandResult,
        apply_command(command, nullptr),
        ++seq_,
        now_seconds());
}

void Session::post_command(CommandMessage message) {
    commands_->push(std::move(message));
}

void Session::post_command(const nlohmann::json& document) {
    CommandMessage message;
    message.document = document;
    commands_->push(std::move(message));
}

void Session::drain_commands(const Sink& sink, ChangeDetector* detector) {
    CommandMessage message;
    while (commands_->try_pop(message)) {
        nlohmann::json result;
        if (message.valid()) {
            result = apply_command(message.document, detector);
        } else {
            result = nlohmann::json{
                {"cmd", ""},
                {"ok", false},
                {"detail", message.error},
            };
        }
        emit(sink, event_type::kCommandResult, std::move(result));
    }
}

nlohmann::json Session::apply_command(
    const nlohmann::json& command,
    ChangeDetector* detector) {
    std::string name;
    if (command.is_object()) {
        const auto found = command.find("cmd");
        if (found != command.end() && found->is_string()) {
            name = found->get<std::string>();
        }
    }
    const auto answer = [&name](bool ok, const std::string& detail) {
        return nlohmann::json{
            {"cmd", name},
            {"ok", ok},
            {"detail", detail},
        };
    };
    if (name.empty()) {
        return answer(false, "command must be an object with a string 'cmd'");
    }

    try {
        if (name == "pause") {
            paused_.store(true);
            return answer(true, "paused");
        }
        if (name == "resume") {
            paused_.store(false);
            return answer(true, "running");
        }
        if (name == "toggle_pause") {
            const bool now_paused = !paused_.load();
            paused_.store(now_paused);
            return answer(true, now_paused ? "paused" : "running");
        }
        if (name == "status") {
            return answer(true, paused_.load() ? "paused" : "running");
        }
        if (name == "shutdown") {
            shutdown_.store(true);
            return answer(true, "shutdown requested");
        }
        if (name == "set_target_lang" || name == "set_scene") {
            const char* key = name == "set_target_lang" ? "target_lang" : "scene";
            std::string value;
            bool have = false;
            for (const char* candidate : {key, "value"}) {
                const auto found = command.find(candidate);
                if (found != command.end() && found->is_string()) {
                    value = found->get<std::string>();
                    have = true;
                    break;
                }
            }
            if (!have) {
                return answer(
                    false,
                    name + " needs a \"" + std::string(key) + "\" or \"value\" string");
            }
            if (name == "set_target_lang" && value.empty()) {
                return answer(false, "set_target_lang needs a non-empty target_lang");
            }
            TranslateContext context = pipeline_.context();
            if (name == "set_target_lang") {
                context.target_lang = value;
            } else {
                context.scene = value;
            }
            pipeline_.set_context(std::move(context));
            // The still-screen cache holds a subtitle translated for the old
            // context; force the next frame through OCR and translation.
            reprocess_ = true;
            return answer(
                true,
                (name == "set_target_lang" ? "target_lang=" : "scene=") + value);
        }
        if (name == "set_fps") {
            const auto found = command.find("value");
            if (found == command.end() || !found->is_number()) {
                return answer(false, "set_fps needs a numeric \"value\"");
            }
            const double value = found->get<double>();
            if (!(value > 0.0) || value > 240.0) {
                return answer(false, "fps must be between 0 and 240");
            }
            fps_ = value;
            return answer(true, "fps=" + std::to_string(value));
        }
        if (name == "set_diff_threshold") {
            const auto found = command.find("value");
            if (found == command.end() || !found->is_number()) {
                return answer(false, "set_diff_threshold needs a numeric \"value\"");
            }
            const double value = found->get<double>();
            if (!(value >= 0.0 && value <= 1.0)) {
                return answer(false, "diff_threshold must be between 0 and 1");
            }
            diff_threshold_ = value;
            if (detector != nullptr) {
                detector->set_threshold(value);
            }
            return answer(true, "diff_threshold=" + std::to_string(value));
        }
        if (name == "reload_corpus") {
            if (!reload_corpus_) {
                return answer(false, "no corpus to reload");
            }
            const std::string detail = reload_corpus_();
            return answer(true, detail.empty() ? "corpus reloaded" : detail);
        }
    } catch (const std::exception& error) {
        return answer(false, error.what());
    }

    return answer(false, "unknown command \"" + name + "\"");
}

Envelope Session::emit(const Sink& sink, std::string_view type, nlohmann::json data) {
    Envelope envelope = encode_event(type, std::move(data), ++seq_, now_seconds());
    if (sink) {
        sink(envelope);
    }
    return envelope;
}

}  // namespace watashi
