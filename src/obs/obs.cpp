#include "obs/obs.hpp"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <system_error>
#include <utility>

#ifdef _WIN32
#include <windows.h>
#include <dbghelp.h>
#endif

#ifndef WATASHI_PRODUCT_NAME
#define WATASHI_PRODUCT_NAME "Project Watashi"
#endif
#ifndef WATASHI_PRODUCT_VERSION
#define WATASHI_PRODUCT_VERSION "0.1.0"
#endif
#ifndef WATASHI_GIT_COMMIT
#define WATASHI_GIT_COMMIT "unknown"
#endif
#ifndef WATASHI_BUILD_TYPE
#define WATASHI_BUILD_TYPE "unknown"
#endif

namespace fs = std::filesystem;

namespace watashi::obs {
namespace {

std::string now_string() {
    std::time_t seconds = std::time(nullptr);
    std::tm local{};
#ifdef _WIN32
    localtime_s(&local, &seconds);
#else
    localtime_r(&seconds, &local);
#endif
    char buffer[32] = {};
    std::snprintf(buffer, sizeof(buffer), "%04d-%02d-%02dT%02d:%02d:%02d",
                  local.tm_year + 1900, local.tm_mon + 1, local.tm_mday,
                  local.tm_hour, local.tm_min, local.tm_sec);
    return buffer;
}

std::string lower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return value;
}

// getenv is flagged as unsafe by MSVC; _dupenv_s copies into memory we own.
std::string environment_value(const char* name) {
#ifdef _WIN32
    char* buffer = nullptr;
    std::size_t length = 0;
    if (_dupenv_s(&buffer, &length, name) != 0 || buffer == nullptr) {
        return {};
    }
    std::string value(buffer);
    std::free(buffer);
    return value;
#else
    const char* value = std::getenv(name);
    return value != nullptr ? std::string(value) : std::string();
#endif
}

fs::path local_app_data() {
    const std::string value = environment_value("LOCALAPPDATA");
    if (value.empty()) {
        return {};
    }
    return fs::path(value);
}

// watashi.log for slot 0, watashi.1.log for slot 1 ... watashi.4.log for slot 4.
fs::path slot_path(const fs::path& directory, std::size_t slot) {
    if (slot == 0) {
        return directory / "watashi.log";
    }
    return directory / ("watashi." + std::to_string(slot) + ".log");
}

// Shift one slot onto the next by replacing it. Replace-on-move means rotation
// never needs a delete, which also keeps it working on hosts that filter
// DeleteFileW.
bool move_replace(const fs::path& from, const fs::path& to) {
#ifdef _WIN32
    return MoveFileExW(from.wstring().c_str(), to.wstring().c_str(),
                       MOVEFILE_REPLACE_EXISTING) != 0;
#else
    std::error_code error;
    fs::remove(to, error);
    error.clear();
    fs::rename(from, to, error);
    return !error;
#endif
}

#ifdef _WIN32
fs::path g_dump_directory;

std::string failure_stamp() {
    std::time_t seconds = std::time(nullptr);
    std::tm local{};
    localtime_s(&local, &seconds);
    char buffer[32] = {};
    std::snprintf(buffer, sizeof(buffer), "%04d%02d%02d-%02d%02d%02d",
                  local.tm_year + 1900, local.tm_mon + 1, local.tm_mday,
                  local.tm_hour, local.tm_min, local.tm_sec);
    return buffer;
}

void write_crash_dump(void* exception_pointers) noexcept {
    if (g_dump_directory.empty()) {
        return;
    }
    const fs::path path = g_dump_directory /
                          dump_file_name(failure_stamp(), GetCurrentProcessId());
    (void)write_dump(path, exception_pointers);
}

LONG WINAPI unhandled_exception_filter(EXCEPTION_POINTERS* information) {
    write_crash_dump(information);
    // Let Windows keep its normal post-mortem behaviour after the file exists.
    return EXCEPTION_CONTINUE_SEARCH;
}

void terminate_handler() {
    write_crash_dump(nullptr);
    std::abort();
}
#endif

}  // namespace

VersionInfo version() {
    return VersionInfo{WATASHI_PRODUCT_NAME, WATASHI_PRODUCT_VERSION,
                       WATASHI_GIT_COMMIT, WATASHI_BUILD_TYPE};
}

std::string version_text() {
    const VersionInfo info = version();
    std::string text;
    text += info.product + " " + info.version + "\n";
    text += "commit " + info.commit + "\n";
    text += "build " + info.build_type + "\n";
    return text;
}

fs::path data_directory() {
    const fs::path root = local_app_data();
    if (root.empty()) {
        return {};
    }
    return root / "ProjectWatashi";
}

fs::path log_directory() {
    const fs::path root = data_directory();
    if (root.empty()) {
        return {};
    }
    return root / "logs";
}

fs::path dump_directory() {
    const fs::path root = data_directory();
    if (root.empty()) {
        return {};
    }
    return root / "dumps";
}

RingLog::RingLog(LogOptions options) : options_(std::move(options)) {
    if (options_.max_bytes < 64) {
        options_.max_bytes = 64;
    }
    if (options_.max_files < 1) {
        options_.max_files = 1;
    }
}

RingLog::~RingLog() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (stream_.is_open()) {
        stream_.close();
    }
}

bool RingLog::open() noexcept {
    std::lock_guard<std::mutex> lock(mutex_);
    if (enabled_) {
        return true;
    }
    if (options_.directory.empty()) {
        return false;
    }
    try {
        std::error_code error;
        fs::create_directories(options_.directory, error);
        if (error) {
            return false;
        }
        path_ = slot_path(options_.directory, 0);
        stream_.open(path_, std::ios::binary | std::ios::app);
        if (!stream_) {
            stream_.clear();
            return false;
        }
        current_bytes_ = static_cast<std::size_t>(fs::file_size(path_, error));
        if (error) {
            current_bytes_ = 0;
        }
        enabled_ = true;
        return true;
    } catch (...) {
        return false;
    }
}

bool RingLog::enabled() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return enabled_;
}

const fs::path& RingLog::path() const {
    return path_;
}

bool RingLog::rotate() noexcept {
    stream_.close();
    stream_.clear();
    // Shift every older file one slot back; the move replaces the file that
    // was in that slot, so the oldest file simply falls off the end.
    for (std::size_t slot = options_.max_files; slot > 1; --slot) {
        const fs::path from = slot_path(options_.directory, slot - 2);
        const fs::path to = slot_path(options_.directory, slot - 1);
        std::error_code error;
        if (!fs::exists(from, error)) {
            continue;
        }
        if (!move_replace(from, to)) {
            return false;
        }
    }
    path_ = slot_path(options_.directory, 0);
    stream_.open(path_, std::ios::binary | std::ios::trunc);
    if (!stream_) {
        stream_.clear();
        return false;
    }
    current_bytes_ = 0;
    return true;
}

bool RingLog::write(const std::string& level, const std::string& message) noexcept {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!enabled_ || !stream_.is_open()) {
        return false;
    }
    try {
        std::string line = now_string() + " [" + level + "] " + message + "\n";
        if (line.size() > options_.max_bytes) {
            line.resize(options_.max_bytes);
            line.back() = '\n';
        }
        if (current_bytes_ + line.size() > options_.max_bytes && current_bytes_ > 0) {
            if (!rotate()) {
                enabled_ = false;
                return false;
            }
        }
        stream_.write(line.data(), static_cast<std::streamsize>(line.size()));
        if (!stream_) {
            stream_.clear();
            enabled_ = false;
            return false;
        }
        stream_.flush();
        current_bytes_ += line.size();
        return true;
    } catch (...) {
        enabled_ = false;
        return false;
    }
}

bool dumps_enabled() {
    const std::string value = environment_value("WATASHI_DUMPS");
    if (value.empty()) {
        return false;
    }
    const std::string text = lower(value);
    return text == "1" || text == "true" || text == "yes";
}

std::string dump_file_name(const std::string& stamp, std::uint32_t process_id) {
    return stamp + "-watashi-" + std::to_string(process_id) + ".dmp";
}

bool write_dump(const fs::path& path, void* exception_pointers) noexcept {
#ifdef _WIN32
    try {
        std::error_code error;
        if (!path.parent_path().empty()) {
            fs::create_directories(path.parent_path(), error);
            if (error) {
                return false;
            }
        }
        HANDLE file = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr,
                                  CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file == INVALID_HANDLE_VALUE) {
            return false;
        }
        EXCEPTION_POINTERS* pointers =
            reinterpret_cast<EXCEPTION_POINTERS*>(exception_pointers);
        MINIDUMP_EXCEPTION_INFORMATION information{};
        information.ThreadId = GetCurrentThreadId();
        information.ExceptionPointers = pointers;
        information.ClientPointers = FALSE;
        const MINIDUMP_TYPE type = static_cast<MINIDUMP_TYPE>(MiniDumpNormal);
        const BOOL written = MiniDumpWriteDump(
            GetCurrentProcess(), GetCurrentProcessId(), file, type,
            pointers != nullptr ? &information : nullptr, nullptr, nullptr);
        CloseHandle(file);
        return written == TRUE;
    } catch (...) {
        return false;
    }
#else
    (void)path;
    (void)exception_pointers;
    return false;
#endif
}

bool install_crash_dump_handler(const fs::path& directory) noexcept {
#ifdef _WIN32
    if (directory.empty()) {
        return false;
    }
    try {
        std::error_code error;
        fs::create_directories(directory, error);
        if (error) {
            return false;
        }
        g_dump_directory = directory;
        SetUnhandledExceptionFilter(unhandled_exception_filter);
        std::set_terminate(terminate_handler);
        return true;
    } catch (...) {
        return false;
    }
#else
    (void)directory;
    return false;
#endif
}

}  // namespace watashi::obs
