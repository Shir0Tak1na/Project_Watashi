// The stdin command channel: one JSON command per line.
//
// The wire shape is the one prototype/watashi/events.py ``encode_command``
// produces: an object with a ``cmd`` string plus optional fields. Parsing is
// deliberately total -- a bad line becomes a message with ``error`` set so the
// session can answer with ``ok: false`` instead of taking the process down.
//
// ``CommandQueue`` is the only shared state between the stdin reader and the
// run loop; it is a plain mutex-protected deque, so a command thread never
// blocks the event writer and the event writer never blocks on stdin.

#pragma once

#include <cstddef>
#include <deque>
#include <mutex>
#include <string>
#include <string_view>

#include <nlohmann/json.hpp>

namespace watashi {

struct CommandMessage {
    /// Parsed command object. Empty when ``error`` is set.
    nlohmann::json document;
    /// Non-empty for a malformed line; the session answers ``ok: false``.
    std::string error;

    [[nodiscard]] bool valid() const noexcept { return error.empty(); }
};

/// Parse one line. Never throws.
[[nodiscard]] CommandMessage parse_command_line(std::string_view line);

class CommandQueue {
public:
    void push(CommandMessage message);

    /// Pops one message. False when the queue is empty.
    [[nodiscard]] bool try_pop(CommandMessage& message);

    [[nodiscard]] std::size_t size() const;

private:
    mutable std::mutex mutex_;
    std::deque<CommandMessage> queue_;
};

}  // namespace watashi
