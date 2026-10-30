// The engine as a child process, read as NDJSON.
//
// The UI owns no recognition, no model and no translation: it spawns
// ``watashi_cli.exe --json-lines`` and reads stdout. This class is the whole
// transport: pipes, a reader thread, a stderr tail and the exit code.
//
// The reader runs off the window thread, and the exit callback only fires
// after both pipes reached EOF, so "last frame + status" is always ordered:
// the window never sees an exit code before the lines that preceded it.

#pragma once

#include <cstddef>
#include <functional>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace watashi::ui {

class ChildStream {
public:
    struct Options {
        std::string executable;
        std::vector<std::string> arguments;
        /// Empty inherits the parent's directory.
        std::string working_directory;
        /// Lines of stderr kept for the status bar. The rest is discarded, so
        /// a chatty child cannot grow the UI's memory.
        std::size_t stderr_tail_lines = 6;
    };

    using LineHandler = std::function<void(const std::string& line)>;
    /// ``launch_failed`` is currently always false here: a process that could
    /// not be started makes ``start`` return false instead.
    using ExitHandler = std::function<void(int exit_code, const std::string& stderr_tail)>;

    explicit ChildStream(Options options);
    ~ChildStream();

    ChildStream(const ChildStream&) = delete;
    ChildStream& operator=(const ChildStream&) = delete;

    /// Starts the process and the readers. False when CreateProcessW failed;
    /// ``error`` says why. No callback fires in that case.
    bool start(LineHandler on_line, ExitHandler on_exit);

    /// Terminates the child and waits for the readers. Safe to call twice.
    void stop();

    [[nodiscard]] bool running() const;
    [[nodiscard]] std::string error() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace watashi::ui
