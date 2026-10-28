// The engine's UI boundary, ported shape-for-shape from
// prototype/watashi/events.py.
//
// The envelope is {v, type, ts, seq, data}. ``v`` and ``type`` are pinned, the
// whitelist of types is closed, and ``seq`` must strictly increase, because
// those are the fields a client keys on. Payload objects are deliberately open:
// a later revision may add keys (segments, timings) that this reader ignores.
//
// This header is pure data. It knows nothing about OCR, translation or a
// window, which is the same rule the Python module follows.

#pragma once

#include <cstdint>
#include <ostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json.hpp>

namespace watashi {

/// Must match prototype/watashi/events.py. Bumping it is a breaking change.
inline constexpr int kSchemaVersion = 1;

namespace event_type {
inline constexpr std::string_view kReady = "ready";
inline constexpr std::string_view kSubtitle = "subtitle";
inline constexpr std::string_view kRefinement = "refinement";
inline constexpr std::string_view kStats = "stats";
inline constexpr std::string_view kStatus = "status";
inline constexpr std::string_view kError = "error";
inline constexpr std::string_view kSettings = "settings";
inline constexpr std::string_view kCorrection = "correction";
inline constexpr std::string_view kLibrary = "library";
inline constexpr std::string_view kCommandResult = "command_result";
inline constexpr std::string_view kStopped = "stopped";
inline constexpr std::string_view kPresentation = "presentation";
}  // namespace event_type

/// Raised when an envelope or a command breaks the contract.
class ContractError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

/// One event, exactly as it travels over any surface.
struct Envelope {
    int v = kSchemaVersion;
    std::string type;
    double ts = 0.0;
    std::int64_t seq = 0;
    nlohmann::json data = nlohmann::json::object();
};

/// True for the event names in the Python ALL_EVENTS tuple.
[[nodiscard]] bool is_known_event(std::string_view type);

/// Build a valid envelope. Throws ContractError for an unknown type or seq < 1.
[[nodiscard]] Envelope encode_event(
    std::string_view type,
    nlohmann::json data,
    std::int64_t seq,
    double ts);

/// Validate and unwrap a JSON object. Throws ContractError on any violation.
[[nodiscard]] Envelope decode_envelope(const nlohmann::json& document);

/// The wire form. Keys are sorted, which is what makes two encoders comparable.
[[nodiscard]] nlohmann::json to_json(const Envelope& envelope);

/// One NDJSON record, without the trailing newline.
[[nodiscard]] std::string to_json_line(const Envelope& envelope);

/// Write one NDJSON record, including the newline.
void write_json_line(std::ostream& out, const Envelope& envelope);

/// Current wall clock in seconds, the unit ``ts`` has always used.
[[nodiscard]] double now_seconds();

}  // namespace watashi
