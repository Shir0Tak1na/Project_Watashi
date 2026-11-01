#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <process.h>
#else
#include <unistd.h>
#endif

#include "obs/obs.hpp"

namespace fs = std::filesystem;

namespace {

/// The directory holding this test binary. Some harnesses deny writes to
/// %TEMP%; the build tree is writable by definition, so scratch files live
/// beside the executable the way the fetch tests already do.
fs::path module_directory() {
#ifdef _WIN32
    wchar_t buffer[MAX_PATH] = {};
    const DWORD length = GetModuleFileNameW(nullptr, buffer, MAX_PATH);
    if (length > 0) {
        return fs::path(buffer).parent_path();
    }
#endif
    return fs::current_path();
}

struct TempDirectory {
    TempDirectory() {
        static std::uint64_t counter = 0;
#ifdef _WIN32
        const long process_id = _getpid();
#else
        const long process_id = static_cast<long>(getpid());
#endif
        // ctest runs every TEST_CASE in its own process, so the pid is the part
        // that keeps parallel cases out of each other's directory.
        path = module_directory() /
               ("watashi-obs-test-" + std::to_string(process_id) + "-" +
                std::to_string(++counter) + "-" +
                std::to_string(static_cast<unsigned long>(std::rand())));
        fs::create_directories(path);
    }
    ~TempDirectory() {
        std::error_code error;
        fs::remove_all(path, error);
    }
    fs::path path;
};

std::string read_text(const fs::path& path) {
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        return {};
    }
    return std::string(std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>());
}

std::size_t count_slot_files(const fs::path& directory) {
    std::size_t count = 0;
    for (const auto& entry : fs::directory_iterator(directory)) {
        if (entry.is_regular_file() && entry.path().extension() == ".log") {
            ++count;
        }
    }
    return count;
}

std::string environment_value(const char* name) {
    char* buffer = nullptr;
    std::size_t length = 0;
    if (_dupenv_s(&buffer, &length, name) != 0 || buffer == nullptr) {
        return {};
    }
    std::string value(buffer);
    std::free(buffer);
    return value;
}

class EnvironmentVariable {
public:
    EnvironmentVariable(const char* name, const char* value) : name_(name) {
        const std::string previous = environment_value(name);
        if (!previous.empty()) {
            had_value_ = true;
            previous_ = previous;
        }
        set(value);
    }
    ~EnvironmentVariable() { set(had_value_ ? previous_.c_str() : nullptr); }

private:
    void set(const char* value) {
        if (value != nullptr) {
            _putenv_s(name_.c_str(), value);
        } else {
            _putenv_s(name_.c_str(), "");
        }
    }
    std::string name_;
    std::string previous_;
    bool had_value_ = false;
};

}  // namespace

TEST_CASE("version text names the product, the commit and the build type") {
    const watashi::obs::VersionInfo info = watashi::obs::version();
    REQUIRE_FALSE(info.product.empty());
    REQUIRE_FALSE(info.version.empty());
    REQUIRE_FALSE(info.commit.empty());
    REQUIRE_FALSE(info.build_type.empty());

    const std::string text = watashi::obs::version_text();
    REQUIRE(text.find(info.product + " " + info.version) != std::string::npos);
    REQUIRE(text.find("commit " + info.commit) != std::string::npos);
    REQUIRE(text.find("build " + info.build_type) != std::string::npos);
    // Three lines, no trailing blank: --version is read by scripts.
    REQUIRE(std::count(text.begin(), text.end(), '\n') == 3);
}

TEST_CASE("the data, log and dump directories stay under the per-user root") {
    TempDirectory temp;
    EnvironmentVariable local_app_data("LOCALAPPDATA", temp.path.string().c_str());

    const fs::path data = watashi::obs::data_directory();
    REQUIRE_FALSE(data.empty());
    REQUIRE(data.filename() == "ProjectWatashi");
    REQUIRE(watashi::obs::log_directory() == data / "logs");
    REQUIRE(watashi::obs::dump_directory() == data / "dumps");
}

TEST_CASE("the log ring keeps at most five files and drops the oldest lines") {
    TempDirectory temp;
    watashi::obs::LogOptions options;
    options.directory = temp.path;
    options.max_bytes = 256;
    options.max_files = 5;

    watashi::obs::RingLog log(options);
    REQUIRE(log.open());
    REQUIRE(log.path().filename() == "watashi.log");
    for (int index = 0; index < 200; ++index) {
        REQUIRE(log.write("info", "marker-" + std::to_string(index) +
                                      "-0123456789012345678901234567890123456789"));
    }

    REQUIRE(count_slot_files(temp.path) <= 5);
    REQUIRE_FALSE(fs::exists(temp.path / "watashi.5.log"));
    REQUIRE(fs::exists(temp.path / "watashi.log"));

    const std::string current = read_text(temp.path / "watashi.log");
    REQUIRE(current.find("marker-199-") != std::string::npos);
    REQUIRE(current.find("marker-0-") == std::string::npos);

    // The first line has been evicted from the whole ring; only the newest
    // five files survive.
    bool first_line_survives = false;
    for (std::size_t slot = 1; slot < 5; ++slot) {
        const fs::path file = temp.path / ("watashi." + std::to_string(slot) + ".log");
        if (fs::exists(file) && read_text(file).find("marker-0-") != std::string::npos) {
            first_line_survives = true;
        }
    }
    REQUIRE_FALSE(first_line_survives);

    // Every surviving file respects the cap it was given.
    for (const auto& entry : fs::directory_iterator(temp.path)) {
        if (entry.is_regular_file()) {
            REQUIRE(fs::file_size(entry.path()) <= options.max_bytes);
        }
    }
}

TEST_CASE("a log that cannot be opened is silent and never throws") {
    TempDirectory temp;
    const fs::path blocker = temp.path / "blocked";
    std::ofstream(blocker) << "not a directory";

    watashi::obs::LogOptions options;
    options.directory = blocker;
    watashi::obs::RingLog log(options);
    REQUIRE_FALSE(log.open());
    REQUIRE_FALSE(log.enabled());
    REQUIRE_FALSE(log.write("info", "this must not throw or escape"));
    REQUIRE_FALSE(log.write("info", "second write is equally quiet"));
}

TEST_CASE("the logger creates its directory and opens watashi.log on first use") {
    TempDirectory temp;
    const fs::path logs = temp.path / "logs";
    REQUIRE_FALSE(fs::exists(logs));
    watashi::obs::LogOptions options;
    options.directory = logs;
    watashi::obs::RingLog log(options);
    REQUIRE(log.open());
    REQUIRE(log.enabled());
    REQUIRE(fs::is_directory(logs));
    REQUIRE(fs::exists(logs / "watashi.log"));
    REQUIRE(log.write("info", "created on demand"));
    REQUIRE(read_text(logs / "watashi.log").find("created on demand") != std::string::npos);
}

TEST_CASE("dump names carry the stamp, the process id and the dmp suffix") {
    const std::string name = watashi::obs::dump_file_name("20261101-120000", 4242);
    REQUIRE(name == "20261101-120000-watashi-4242.dmp");
    REQUIRE(name.find(".dmp") != std::string::npos);
    REQUIRE(name.find("4242") != std::string::npos);
}

TEST_CASE("dumps are local and opt-in through WATASHI_DUMPS") {
    {
        EnvironmentVariable dumps("WATASHI_DUMPS", nullptr);
        REQUIRE_FALSE(watashi::obs::dumps_enabled());
    }
    {
        EnvironmentVariable dumps("WATASHI_DUMPS", "1");
        REQUIRE(watashi::obs::dumps_enabled());
    }
    {
        EnvironmentVariable dumps("WATASHI_DUMPS", "yes");
        REQUIRE(watashi::obs::dumps_enabled());
    }
    {
        EnvironmentVariable dumps("WATASHI_DUMPS", "0");
        REQUIRE_FALSE(watashi::obs::dumps_enabled());
    }
}

TEST_CASE("a minidump of the current process is written where asked") {
    TempDirectory temp;
    const fs::path path = temp.path / watashi::obs::dump_file_name("20261101-120000", 4242);
    REQUIRE(watashi::obs::write_dump(path, nullptr));
    REQUIRE(fs::exists(path));
    REQUIRE(fs::file_size(path) > 0);
}

TEST_CASE("a crash handler needs a directory and never touches the filter before that") {
    // install_crash_dump_handler installs a process-wide filter; refusing an
    // empty path keeps it from being installed by accident. A live crash is
    // exercised by write_dump's own test instead of crashing the test host.
    REQUIRE_FALSE(watashi::obs::install_crash_dump_handler({}));
}
