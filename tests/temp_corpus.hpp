// A throwaway corpus tree for one test.
#pragma once

#include <chrono>
#include <filesystem>
#include <fstream>
#include <random>
#include <string>

#include "watashi/corpus.hpp"

namespace watashi::test {

class TempCorpus {
public:
    TempCorpus() {
        std::random_device device;
        std::mt19937 generator(device());
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        root_ = std::filesystem::current_path() /
                ("watashi_corpus_" + std::to_string(stamp) + "_" +
                 std::to_string(generator()));
        std::filesystem::create_directories(root_);
        for (const char* layer : {"user", "domain", "general"}) {
            std::filesystem::create_directories(root_ / layer);
        }
    }

    ~TempCorpus() {
        std::error_code error;
        std::filesystem::remove_all(root_, error);
    }

    TempCorpus(const TempCorpus&) = delete;
    TempCorpus& operator=(const TempCorpus&) = delete;

    std::string write(const std::string& relative, const std::string& contents) const {
        const std::filesystem::path path = root_ / relative;
        std::filesystem::create_directories(path.parent_path());
        std::ofstream file(path, std::ios::binary | std::ios::trunc);
        file << contents;
        file.close();
        return path.string();
    }

    [[nodiscard]] std::string path(const std::string& relative) const {
        return (root_ / relative).string();
    }

    [[nodiscard]] std::string root() const { return root_.string(); }

private:
    std::filesystem::path root_;
};

inline CorpusStore::Options options_for(
    const TempCorpus& corpus,
    bool auto_reload = false,
    double reload_interval_s = 0.5) {
    CorpusStore::Options options;
    options.user_dirs = {corpus.path("user")};
    options.domain_dirs = {corpus.path("domain")};
    options.general_dirs = {corpus.path("general")};
    options.auto_reload = auto_reload;
    options.reload_interval_s = reload_interval_s;
    return options;
}

}  // namespace watashi::test
