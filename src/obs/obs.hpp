#pragma once

// W4.3 observability: version stamping, a size-capped local log ring and
// optional local minidumps. Nothing here talks to the network, and nothing
// records subtitle text: callers log metadata only.

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <string>

namespace watashi::obs {

/// Compile-time stamp. The CMake target defines the four macros; the fallbacks
/// keep a plain translation unit compilable.
struct VersionInfo {
    std::string product;
    std::string version;
    std::string commit;
    std::string build_type;
};

VersionInfo version();
/// Three lines: product + version, commit, build type. Used by --version.
std::string version_text();

/// %LOCALAPPDATA%\ProjectWatashi, or an empty path when LOCALAPPDATA is unset.
std::filesystem::path data_directory();
/// data_directory()\logs.
std::filesystem::path log_directory();
/// data_directory()\dumps.
std::filesystem::path dump_directory();

// --------------------------------------------------------------------------- //
// log ring
// --------------------------------------------------------------------------- //
//
// Five files, 2 MiB each by default: watashi.log is current, watashi.1.log is
// the previous one, down to watashi.4.log. A failed open or write disables the
// logger quietly; logging must never take the overlay down.
struct LogOptions {
    std::filesystem::path directory;
    std::size_t max_bytes = 2u * 1024u * 1024u;
    std::size_t max_files = 5;
};

class RingLog {
public:
    explicit RingLog(LogOptions options);
    ~RingLog();

    RingLog(const RingLog&) = delete;
    RingLog& operator=(const RingLog&) = delete;

    /// Creates the directory and opens watashi.log in append mode. Returns
    /// false when the filesystem refuses; never throws.
    bool open() noexcept;
    bool enabled() const;
    const std::filesystem::path& path() const;
    /// Writes "<timestamp> [level] message". Never throws, never blocks the
    /// caller on a bad disk. Returns false when nothing was written.
    bool write(const std::string& level, const std::string& message) noexcept;

private:
    bool rotate() noexcept;

    LogOptions options_;
    std::filesystem::path path_;
    std::ofstream stream_;
    std::size_t current_bytes_ = 0;
    bool enabled_ = false;
    mutable std::mutex mutex_;
};

// --------------------------------------------------------------------------- //
// minidumps
// --------------------------------------------------------------------------- //
//
// Dumps are local only and are never uploaded. They are off by default because
// a dump of an overlay process can contain captured screen text; set
// WATASHI_DUMPS=1 to opt in for a support run.
bool dumps_enabled();
/// "%Y%m%d-%H%M%S-watashi-<pid>.dmp"; exposed so the naming can be tested.
std::string dump_file_name(const std::string& stamp, std::uint32_t process_id);
bool write_dump(const std::filesystem::path& path, void* exception_pointers) noexcept;
/// Installs a SetUnhandledExceptionFilter/terminate pair that writes one dump
/// into `directory`. Callers must check dumps_enabled() first; returns false
/// when the handler could not be installed.
bool install_crash_dump_handler(const std::filesystem::path& directory) noexcept;

}  // namespace watashi::obs
