// The overlay's view of the engine's JSONL stream.
//
// This layer does exactly one thing: turn one NDJSON record into something the
// window can paint. It reuses the engine's own envelope decoder so the UI and
// the engine cannot disagree about what a valid event is, and it keeps only the
// latest subtitle frame plus counters -- no history, because a five-minute run
// must not grow memory with every line.
//
// Everything here is thread-safe: the child process reader calls ``ingest``
// from its own thread while the window thread takes snapshots.

#pragma once

#include <cstdint>
#include <mutex>
#include <string>
#include <string_view>

namespace watashi::ui {

enum class StreamLineKind {
    /// A subtitle event with a usable target line.
    kSubtitle,
    /// A valid event of another type, or an empty line.
    kIgnored,
    /// Not an event at all: bad JSON, a contract violation, a subtitle with no
    /// target. Counted, never shown as content.
    kMalformed,
};

/// The part of ``subtitle.data`` the overlay paints in W3.0.
struct SubtitleFrame {
    std::string source;
    std::string target;
    std::string backend;
    std::int64_t seq = 0;
    double latency_ms = 0.0;
    bool valid = false;
};

struct StreamLine {
    StreamLineKind kind = StreamLineKind::kIgnored;
    SubtitleFrame frame;
    /// Envelope seq when the record decoded far enough to carry one.
    std::int64_t seq = 0;
    std::string type;
    std::string error;
};

/// Parse one NDJSON record. Never throws: a broken line becomes
/// ``kMalformed`` with ``error`` filled in.
[[nodiscard]] StreamLine parse_stream_line(std::string_view line);

/// Strictly increasing seconds on a monotonic clock. Used for the watchdog and
/// for nothing the user sees.
[[nodiscard]] double steady_seconds();

/// Latest frame plus stream health. A value copy, safe to hand to the painter.
struct UiSnapshot {
    SubtitleFrame frame;
    bool has_frame = false;
    std::int64_t last_seq = 0;

    std::size_t subtitles = 0;
    std::size_t malformed = 0;
    std::size_t ignored = 0;
    std::size_t out_of_order = 0;
    /// Records missing between two seq values (``current - last - 1``).
    std::size_t missing_records = 0;

    std::string last_error;
    std::string stderr_tail;
    bool stream_ended = false;
    bool launch_failed = false;
    int exit_code = -1;

    /// Bumped on every accepted change; the window repaints only when it moves.
    std::uint64_t version = 0;
    double started_seconds = 0.0;
    double last_line_seconds = 0.0;
};

class UiModel {
public:
    UiModel();

    /// Feed one line from the child process. Returns the parsed line so a
    /// caller can log a bad one without taking a second snapshot.
    StreamLine ingest(std::string_view line);

    /// The process ended (normally or not) or the pipe closed.
    void end_stream(int exit_code, std::string stderr_tail, bool launch_failed);

    /// Used when the child could not be started at all.
    void set_launch_failure(std::string error);

    [[nodiscard]] UiSnapshot snapshot() const;

private:
    void observe_seq(std::int64_t seq, UiSnapshot& state);

    mutable std::mutex mutex_;
    UiSnapshot state_;
};

}  // namespace watashi::ui
