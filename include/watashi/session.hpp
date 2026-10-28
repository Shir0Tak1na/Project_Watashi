// A session owns the event stream: it emits ``ready`` first, one ``subtitle``
// and ``stats`` per synthetic frame, and ``stopped`` last. Nothing else here
// touches a screen, a model or a thread -- the walking skeleton exists to pin
// the order and the sequence, not the recognition.
//
// The guarantees live in one place because they are what every surface depends
// on: ``seq`` strictly increases, ``ready`` is first, ``stopped`` is last.

#pragma once

#include <cstdint>
#include <functional>
#include <string>
#include <string_view>

#include "watashi/events.hpp"
#include "watashi/pipeline.hpp"

namespace watashi {

/// What the synthetic run should produce. Duration is in seconds and frames are
/// ``duration_s * fps``, rounded, so 2 s at 10 fps is 20 subtitles.
struct SessionOptions {
    double duration_s = 2.0;
    double fps = 10.0;
    /// The synthetic region the ready event describes. Real capture will replace
    /// this, but a client that draws a box needs the origin and size from
    /// somewhere today.
    int region_width = 1200;
    int region_height = 200;
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

    [[nodiscard]] bool paused() const noexcept { return paused_; }
    [[nodiscard]] std::int64_t last_seq() const noexcept { return seq_; }
    [[nodiscard]] int frames() const noexcept { return frames_; }

private:
    Envelope emit(const Sink& sink, std::string_view type, nlohmann::json data);

    SessionOptions options_;
    Pipeline pipeline_;
    std::int64_t seq_ = 0;
    int frames_ = 0;
    bool paused_ = false;
};

}  // namespace watashi
