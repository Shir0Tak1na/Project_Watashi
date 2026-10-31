#include "watashi/commands.hpp"

#include <string>
#include <utility>

namespace watashi {
namespace {

std::string trim(std::string_view text) {
    std::size_t begin = 0;
    std::size_t end = text.size();
    while (begin < end) {
        const char character = text[begin];
        if (character != ' ' && character != '\t' && character != '\r' &&
            character != '\n') {
            break;
        }
        ++begin;
    }
    while (end > begin) {
        const char character = text[end - 1];
        if (character != ' ' && character != '\t' && character != '\r' &&
            character != '\n') {
            break;
        }
        --end;
    }
    return std::string(text.substr(begin, end - begin));
}

}  // namespace

CommandMessage parse_command_line(std::string_view line) {
    CommandMessage message;
    const std::string text = trim(line);
    if (text.empty()) {
        message.error = "empty command";
        return message;
    }

    nlohmann::json document;
    try {
        document = nlohmann::json::parse(text.begin(), text.end());
    } catch (const nlohmann::json::exception& error) {
        message.error = std::string("bad JSON: ") + error.what();
        return message;
    }
    if (!document.is_object()) {
        message.error = "command must be a JSON object";
        return message;
    }
    message.document = std::move(document);
    return message;
}

void CommandQueue::push(CommandMessage message) {
    std::lock_guard<std::mutex> lock(mutex_);
    queue_.push_back(std::move(message));
}

bool CommandQueue::try_pop(CommandMessage& message) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (queue_.empty()) {
        return false;
    }
    message = std::move(queue_.front());
    queue_.pop_front();
    return true;
}

std::size_t CommandQueue::size() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return queue_.size();
}

}  // namespace watashi
