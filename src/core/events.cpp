#include "watashi/events.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <string>
#include <utility>

namespace watashi {
namespace {

constexpr std::array<std::string_view, 12> kAllEvents = {
    event_type::kReady,       event_type::kSubtitle,    event_type::kRefinement,
    event_type::kStats,       event_type::kStatus,      event_type::kError,
    event_type::kSettings,    event_type::kCorrection,  event_type::kLibrary,
    event_type::kCommandResult, event_type::kStopped,   event_type::kPresentation,
};

const nlohmann::json& require_key(const nlohmann::json& document, const char* key) {
    const auto found = document.find(key);
    if (found == document.end()) {
        throw ContractError(std::string("event is missing '") + key + "'");
    }
    return *found;
}

}  // namespace

bool is_known_event(std::string_view type) {
    return std::find(kAllEvents.begin(), kAllEvents.end(), type) != kAllEvents.end();
}

Envelope encode_event(
    std::string_view type,
    nlohmann::json data,
    std::int64_t seq,
    double ts) {
    if (!is_known_event(type)) {
        throw ContractError("unknown event type " + std::string(type));
    }
    if (seq < 1) {
        throw ContractError("seq must be >= 1, got " + std::to_string(seq));
    }
    Envelope envelope;
    envelope.type = std::string(type);
    envelope.ts = ts;
    envelope.seq = seq;
    envelope.data = std::move(data);
    return envelope;
}

Envelope decode_envelope(const nlohmann::json& document) {
    if (!document.is_object()) {
        throw ContractError("event must be an object");
    }
    try {
        const nlohmann::json& version = require_key(document, "v");
        if (!version.is_number_integer() || version.get<int>() != kSchemaVersion) {
            throw ContractError(
                "unsupported schema version " + version.dump() +
                " (this build speaks " + std::to_string(kSchemaVersion) + ")");
        }

        const nlohmann::json& type = require_key(document, "type");
        if (!type.is_string()) {
            throw ContractError("event type must be a string");
        }
        const std::string name = type.get<std::string>();
        if (!is_known_event(name)) {
            throw ContractError("unknown event type " + name);
        }

        const nlohmann::json& ts = require_key(document, "ts");
        if (!ts.is_number()) {
            throw ContractError("ts must be a number");
        }

        const nlohmann::json& data = require_key(document, "data");
        if (!data.is_object()) {
            throw ContractError("event data must be an object");
        }

        Envelope envelope;
        envelope.type = name;
        envelope.ts = ts.get<double>();
        envelope.data = data;

        const auto seq = document.find("seq");
        if (seq != document.end()) {
            if (!seq->is_number_integer() || seq->get<std::int64_t>() < 1) {
                throw ContractError("seq must be a positive integer");
            }
            envelope.seq = seq->get<std::int64_t>();
        }
        return envelope;
    } catch (const nlohmann::json::exception& error) {
        throw ContractError(std::string("malformed event: ") + error.what());
    }
}

nlohmann::json to_json(const Envelope& envelope) {
    return nlohmann::json{
        {"v", envelope.v},
        {"type", envelope.type},
        {"ts", envelope.ts},
        {"seq", envelope.seq},
        {"data", envelope.data},
    };
}

double now_seconds() {
    return std::chrono::duration<double>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

}  // namespace watashi
