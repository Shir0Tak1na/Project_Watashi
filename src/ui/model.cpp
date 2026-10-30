#include "watashi/ui/model.hpp"

#include <algorithm>
#include <chrono>
#include <stdexcept>
#include <string>
#include <utility>

#include <nlohmann/json.hpp>

#include "watashi/events.hpp"

namespace watashi::ui {
namespace {

const nlohmann::json* find_string(
    const nlohmann::json& data,
    const char* key) {
    const auto found = data.find(key);
    if (found == data.end() || !found->is_string()) {
        return nullptr;
    }
    return &*found;
}

StreamLine malformed(std::string error) {
    StreamLine line;
    line.kind = StreamLineKind::kMalformed;
    line.error = std::move(error);
    return line;
}

}  // namespace

double steady_seconds() {
    return std::chrono::duration<double>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

StreamLine parse_stream_line(std::string_view raw) {
    StreamLine result;

    std::string text(raw);
    if (!text.empty() && text.back() == '\r') {
        text.pop_back();
    }
    const auto first = text.find_first_not_of(" \t");
    if (first == std::string::npos) {
        result.kind = StreamLineKind::kIgnored;
        return result;
    }
    if (first != 0) {
        text.erase(0, first);
    }

    nlohmann::json document;
    try {
        document = nlohmann::json::parse(text);
    } catch (const nlohmann::json::exception& error) {
        return malformed(std::string("bad JSON: ") + error.what());
    }

    Envelope envelope;
    try {
        envelope = decode_envelope(document);
    } catch (const ContractError& error) {
        return malformed(error.what());
    }

    result.seq = envelope.seq;
    result.type = envelope.type;
    if (envelope.type != event_type::kSubtitle) {
        result.kind = StreamLineKind::kIgnored;
        return result;
    }

    const auto* target = find_string(envelope.data, "target");
    if (target == nullptr) {
        return malformed("subtitle event without a string 'target'");
    }

    SubtitleFrame frame;
    frame.target = target->get<std::string>();
    if (const auto* source = find_string(envelope.data, "source")) {
        frame.source = source->get<std::string>();
    }
    if (const auto* backend = find_string(envelope.data, "backend")) {
        frame.backend = backend->get<std::string>();
    }
    frame.seq = envelope.seq;
    if (const auto found = envelope.data.find("latency_ms");
        found != envelope.data.end() && found->is_number()) {
        frame.latency_ms = found->get<double>();
    }
    frame.valid = true;

    result.kind = StreamLineKind::kSubtitle;
    result.frame = std::move(frame);
    return result;
}

UiModel::UiModel() {
    state_.started_seconds = steady_seconds();
    state_.last_line_seconds = state_.started_seconds;
}

void UiModel::observe_seq(std::int64_t seq, UiSnapshot& state) {
    if (seq < 1) {
        return;
    }
    if (state.last_seq == 0) {
        state.last_seq = seq;
        return;
    }
    if (seq <= state.last_seq) {
        ++state.out_of_order;
        return;
    }
    if (seq > state.last_seq + 1) {
        state.missing_records +=
            static_cast<std::size_t>(seq - state.last_seq - 1);
    }
    state.last_seq = seq;
}

StreamLine UiModel::ingest(std::string_view line) {
    StreamLine parsed = parse_stream_line(line);

    std::lock_guard<std::mutex> lock(mutex_);
    state_.last_line_seconds = steady_seconds();
    ++state_.version;
    observe_seq(parsed.seq, state_);

    switch (parsed.kind) {
        case StreamLineKind::kSubtitle:
            ++state_.subtitles;
            state_.frame = parsed.frame;
            state_.has_frame = true;
            break;
        case StreamLineKind::kIgnored:
            ++state_.ignored;
            break;
        case StreamLineKind::kMalformed:
            ++state_.malformed;
            state_.last_error = parsed.error;
            break;
    }
    return parsed;
}

void UiModel::end_stream(
    int exit_code,
    std::string stderr_tail,
    bool launch_failed) {
    std::lock_guard<std::mutex> lock(mutex_);
    state_.stream_ended = true;
    state_.exit_code = exit_code;
    state_.stderr_tail = std::move(stderr_tail);
    state_.launch_failed = state_.launch_failed || launch_failed;
    ++state_.version;
}

void UiModel::set_launch_failure(std::string error) {
    std::lock_guard<std::mutex> lock(mutex_);
    state_.stream_ended = true;
    state_.launch_failed = true;
    state_.exit_code = -1;
    state_.last_error = std::move(error);
    ++state_.version;
}

UiSnapshot UiModel::snapshot() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return state_;
}

}  // namespace watashi::ui
