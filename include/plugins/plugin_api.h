#pragma once

#include <string>

namespace projectwatashi {

class IPlugin {
public:
    virtual ~IPlugin() = default;
    virtual std::string name() const = 0;
    virtual bool initialize() = 0;
    virtual std::string process(const std::string& text,
                               const std::string& src_lang,
                               const std::string& dst_lang) = 0;
    virtual void shutdown() = 0;
};

} // namespace projectwatashi
