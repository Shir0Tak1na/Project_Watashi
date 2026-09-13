#pragma once

#include <string>

namespace projectwatashi {

class LlmBackend {
public:
    virtual ~LlmBackend() = default;
    virtual bool initialize() = 0;
    virtual std::string generate(const std::string& prompt) = 0;
    virtual void shutdown() = 0;
};

} // namespace projectwatashi
