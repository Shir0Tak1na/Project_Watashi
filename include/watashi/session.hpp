// A session owns the event stream: it emits ``ready`` first, one ``subtitle``
// and ``stats`` per synthetic frame, and ``stopped`` last. Nothing else here
// touches a screen, a model or a thread -- the walking skeleton exists to pin
// the order and the sequence, not the recognition.
//
// The guarantees live in one place because they are what every surface depends
// on: ``seq`` strictly increases, ``ready`` is first, ``stopped`` is last.

#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <string_view>

#include "watashi/capture.hpp"
#include "watashi/commands.hpp"
#include "watashi/events.hpp"
#include "watashi/pipeline.hpp"

namespace watashi {

/// What a run should produce. Duration is in seconds and frames are
/// ``duration_s * fps``, rounded, so 2 s at 10 fps is 20 subtitles.
///
/// With ``capture`` set the pixels come from that source and the loop is paced
/// at ``fps``; without it the session synthesises one blank frame per tick,
/// which is the CI default and keeps every existing stream identical.
struct SessionOptions {
    double duration_s = 2.0;
    double fps = 10.0;
    /// The synthetic region the ready event describes when ``capture`` is
    /// null. A capture source reports its own.
    int region_width = 1200;
    int region_height = 200;
    /// Non-owning: the caller keeps the source alive for the whole run.
    CaptureSource* capture = nullptr;
    /// ``threshold > 0`` turns on the still-screen check: an unchanged frame
    /// skips OCR and is counted in ``stats.skipped``.
    DiffOptions diff;
    /// What ``ready`` and ``stats`` call the translator ("fake", "corpus").
    std::string backend_label = "fake";
    /// Target language, scene and window handed to the translator.
    TranslateContext context;
    /// Write capture/end-to-end percentiles here after the run, when set.
    std::string bench_path;
    /// Applies ``reload_corpus``. Empty when the run has no corpus; the command
    /// then answers ``ok: false`` rather than pretending to reload nothing.
    std::function<std::string()> reload_corpus;
};

/// Drives a Pipeline and publishes its events in contract order.
class Session {
public:
    using Sink = std::function<void(const Envelope&)>;

    Session(SessionOptions options, const OcrSource& ocr, const Translator& translator);

    /// Emit the whole stream for this session. The sink is called once per
    /// event, in order; it may be empty in the sense of receiving no calls only
    /// if ``run`` itself is not called.
    void run(const Sink& sink);

    /// Handle one command by name. Known commands answer with a
    /// ``command_result`` envelope; anything else raises ContractError, because
    /// silently ignoring a command is how a surface ends up lying about state.
    [[nodiscard]] Envelope handle_command(std::string_view command);

    /// Payload-aware command application, for tests and for the stdin channel.
    /// Answers ``ok: false`` instead of throwing on a bad or unknown command.
    [[nodiscard]] Envelope handle_command_json(const nlohmann::json& command);

    /// Thread-safe: the stdin reader posts here while the run loop consumes.
    void post_command(CommandMessage message);
    void post_command(const nlohmann::json& document);

    [[nodiscard]] std::shared_ptr<CommandQueue> command_queue() const noexcept {
        return commands_;
    }

    [[nodiscard]] bool paused() const noexcept { return paused_.load(); }
    [[nodiscard]] bool shutdown_requested() const noexcept {
        return shutdown_.load();
    }
    [[nodiscard]] double fps() const noexcept { return fps_; }
    [[nodiscard]] double diff_threshold() const noexcept { return diff_threshold_; }
    [[nodiscard]] const TranslateContext& context() const noexcept {
        return pipeline_.context();
    }
    [[nodiscard]] std::int64_t last_seq() const noexcept { return seq_; }
    [[nodiscard]] int frames() const noexcept { return frames_; }

private:
    Envelope emit(const Sink& sink, std::string_view type, nlohmann::json data);

    /// Apply one parsed command. ``detector`` is null outside a run; the
    /// threshold is then remembered for the next one.
    [[nodiscard]] nlohmann::json apply_command(
        const nlohmann::json& command,
        ChangeDetector* detector);

    void drain_commands(const Sink& sink, ChangeDetector* detector);

    SessionOptions options_;
    Pipeline pipeline_;
    std::int64_t seq_ = 0;
    int frames_ = 0;
    std::atomic<bool> paused_{false};
    std::atomic<bool> shutdown_{false};
    double fps_ = 10.0;
    double diff_threshold_ = 0.0;
    /// Set when a command changed the translation context: the next captured
    /// frame must be read again even if the screen did not change, because the
    /// cached subtitle was translated for the old target language.
    bool reprocess_ = false;
    std::function<std::string()> reload_corpus_;
    std::shared_ptr<CommandQueue> commands_ = std::make_shared<CommandQueue>();
};

}  // namespace watashi
