#include "watashi/ui/child_stream.hpp"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <atomic>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace watashi::ui {
namespace {

std::wstring widen(const std::string& text) {
    if (text.empty()) {
        return std::wstring();
    }
    const int length = MultiByteToWideChar(
        CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0);
    std::wstring wide(static_cast<std::size_t>(length), L'\0');
    if (length > 0) {
        MultiByteToWideChar(
            CP_UTF8, 0, text.data(), static_cast<int>(text.size()), wide.data(), length);
    }
    return wide;
}

/// Quote one argument for the CreateProcessW command-line parser.
std::wstring quote_argument(const std::wstring& argument) {
    if (argument.empty()) {
        return L"\"\"";
    }
    const bool needs_quotes =
        argument.find_first_of(L" \t\"") != std::wstring::npos;
    if (!needs_quotes) {
        return argument;
    }
    std::wstring quoted = L"\"";
    std::size_t backslashes = 0;
    for (wchar_t character : argument) {
        if (character == L'\\') {
            ++backslashes;
            continue;
        }
        if (character == L'"') {
            quoted.append(backslashes * 2 + 1, L'\\');
            quoted.push_back(L'"');
        } else {
            quoted.append(backslashes, L'\\');
            quoted.push_back(character);
        }
        backslashes = 0;
    }
    quoted.append(backslashes * 2, L'\\');
    quoted.push_back(L'"');
    return quoted;
}

std::string trim_cr(std::string line) {
    if (!line.empty() && line.back() == '\r') {
        line.pop_back();
    }
    return line;
}

}  // namespace

struct ChildStream::Impl {
    Options options;
    std::atomic<HANDLE> process{nullptr};
    DWORD process_id = 0;
    HANDLE stdout_read = nullptr;
    HANDLE stderr_read = nullptr;
    std::thread stdout_thread;
    std::thread stderr_thread;
    std::thread wait_thread;
    std::atomic<bool> running{false};
    std::atomic<bool> stopping{false};
    std::mutex error_mutex;
    std::string error;

    std::mutex tail_mutex;
    std::deque<std::string> stderr_tail;

    void set_error(std::string message) {
        std::lock_guard<std::mutex> lock(error_mutex);
        error = std::move(message);
    }

    std::string get_error() {
        std::lock_guard<std::mutex> lock(error_mutex);
        return error;
    }

    void push_stderr(const std::string& line) {
        std::lock_guard<std::mutex> lock(tail_mutex);
        stderr_tail.push_back(line);
        while (stderr_tail.size() > options.stderr_tail_lines) {
            stderr_tail.pop_front();
        }
    }

    std::string tail() {
        std::lock_guard<std::mutex> lock(tail_mutex);
        std::string joined;
        for (const auto& line : stderr_tail) {
            if (!joined.empty()) {
                joined += " | ";
            }
            joined += line;
        }
        return joined;
    }
};

ChildStream::ChildStream(Options options) : impl_(std::make_unique<Impl>()) {
    impl_->options = std::move(options);
}

ChildStream::~ChildStream() {
    stop();
}

bool ChildStream::start(LineHandler on_line, ExitHandler on_exit) {
    if (impl_->running.load()) {
        return true;
    }

    SECURITY_ATTRIBUTES attributes{};
    attributes.nLength = sizeof(attributes);
    attributes.bInheritHandle = TRUE;

    HANDLE stdout_write = nullptr;
    HANDLE stderr_write = nullptr;
    if (!CreatePipe(&impl_->stdout_read, &stdout_write, &attributes, 0)) {
        impl_->set_error("CreatePipe(stdout) failed: " +
                         std::to_string(GetLastError()));
        return false;
    }
    if (!CreatePipe(&impl_->stderr_read, &stderr_write, &attributes, 0)) {
        CloseHandle(impl_->stdout_read);
        CloseHandle(stdout_write);
        impl_->set_error("CreatePipe(stderr) failed: " +
                         std::to_string(GetLastError()));
        return false;
    }
    SetHandleInformation(impl_->stdout_read, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(impl_->stderr_read, HANDLE_FLAG_INHERIT, 0);

    std::wstring command = quote_argument(widen(impl_->options.executable));
    for (const auto& argument : impl_->options.arguments) {
        command += L' ';
        command += quote_argument(widen(argument));
    }

    STARTUPINFOW startup{};
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdOutput = stdout_write;
    startup.hStdError = stderr_write;
    startup.hStdInput = nullptr;

    PROCESS_INFORMATION process{};
    const std::wstring working_directory = widen(impl_->options.working_directory);
    const BOOL created = CreateProcessW(
        nullptr,
        command.data(),
        nullptr,
        nullptr,
        TRUE,
        CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
        nullptr,
        working_directory.empty() ? nullptr : working_directory.c_str(),
        &startup,
        &process);

    CloseHandle(stdout_write);
    CloseHandle(stderr_write);

    if (!created) {
        const DWORD code = GetLastError();
        CloseHandle(impl_->stdout_read);
        CloseHandle(impl_->stderr_read);
        impl_->stdout_read = nullptr;
        impl_->stderr_read = nullptr;
        impl_->set_error("CreateProcessW failed: " + std::to_string(code));
        return false;
    }

    impl_->process.store(process.hProcess);
    impl_->process_id = process.dwProcessId;
    CloseHandle(process.hThread);
    impl_->running.store(true);
    impl_->stopping.store(false);

    impl_->stdout_thread = std::thread([this, on_line, handle = impl_->stdout_read]() {
        std::string pending;
        char buffer[4096];
        while (true) {
            DWORD read = 0;
            const BOOL ok = ReadFile(handle, buffer, sizeof(buffer), &read, nullptr);
            if (!ok || read == 0) {
                break;
            }
            pending.append(buffer, read);
            std::size_t newline = 0;
            while ((newline = pending.find('\n')) != std::string::npos) {
                std::string line = trim_cr(pending.substr(0, newline));
                pending.erase(0, newline + 1);
                if (!line.empty()) {
                    on_line(line);
                }
            }
        }
        if (!pending.empty()) {
            on_line(trim_cr(pending));
        }
        CloseHandle(handle);
    });

    impl_->stderr_thread = std::thread([this, handle = impl_->stderr_read]() {
        std::string pending;
        char buffer[4096];
        while (true) {
            DWORD read = 0;
            const BOOL ok = ReadFile(handle, buffer, sizeof(buffer), &read, nullptr);
            if (!ok || read == 0) {
                break;
            }
            pending.append(buffer, read);
            std::size_t newline = 0;
            while ((newline = pending.find('\n')) != std::string::npos) {
                const std::string line = trim_cr(pending.substr(0, newline));
                pending.erase(0, newline + 1);
                if (!line.empty()) {
                    impl_->push_stderr(line);
                }
            }
        }
        if (!pending.empty()) {
            impl_->push_stderr(trim_cr(pending));
        }
        CloseHandle(handle);
    });

    impl_->wait_thread = std::thread([this, on_exit]() {
        HANDLE child = impl_->process.load();
        WaitForSingleObject(child, INFINITE);
        DWORD exit_code = 0;
        GetExitCodeProcess(child, &exit_code);
        impl_->process.store(nullptr);
        CloseHandle(child);

        // Drain both pipes before announcing the end: the stream's last
        // subtitle must arrive before the "stream ended" status.
        if (impl_->stdout_thread.joinable()) {
            impl_->stdout_thread.join();
        }
        if (impl_->stderr_thread.joinable()) {
            impl_->stderr_thread.join();
        }
        impl_->running.store(false);

        if (!impl_->stopping.load() && on_exit) {
            on_exit(static_cast<int>(exit_code), impl_->tail());
        }
    });
    return true;
}

void ChildStream::stop() {
    if (!impl_) {
        return;
    }
    impl_->stopping.store(true);
    HANDLE child = impl_->process.load();
    if (child != nullptr) {
        TerminateProcess(child, 1);
    }
    if (impl_->wait_thread.joinable()) {
        impl_->wait_thread.join();
    }
    // The wait thread joins the readers; join again defensively in case start
    // failed after one of them was spawned (it never is, but this must not
    // leave a joinable thread behind).
    if (impl_->stdout_thread.joinable()) {
        impl_->stdout_thread.join();
    }
    if (impl_->stderr_thread.joinable()) {
        impl_->stderr_thread.join();
    }
    impl_->running.store(false);
}

bool ChildStream::running() const {
    return impl_->running.load();
}

std::string ChildStream::error() const {
    std::lock_guard<std::mutex> lock(impl_->error_mutex);
    return impl_->error;
}

}  // namespace watashi::ui
