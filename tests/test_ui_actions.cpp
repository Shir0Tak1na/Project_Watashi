// UI-side command surfaces: hotkey/tray tables, command payloads, the
// command_result envelope in the model, and the child stdin pipe.

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iterator>
#include <mutex>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

#include "watashi/commands.hpp"
#include "watashi/events.hpp"
#include "watashi/ui/actions.hpp"
#include "watashi/ui/child_stream.hpp"
#include "watashi/ui/layered_window.hpp"
#include "watashi/ui/model.hpp"
#include "watashi/ui/tray.hpp"

namespace {

using namespace watashi;

bool wait_until(const std::function<bool()>& predicate, int timeout_ms) {
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::milliseconds(timeout_ms);
    while (std::chrono::steady_clock::now() < deadline) {
        if (predicate()) {
            return true;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return predicate();
}

std::filesystem::path module_directory() {
    wchar_t module[32768]{};
    GetModuleFileNameW(nullptr, module, 32768);
    return std::filesystem::path(module).parent_path();
}

std::string sibling(const std::string& name) {
    return (module_directory() / std::filesystem::path(name)).string();
}

/// The CLI and the UI are built in build-*/Release, two directories above the
/// test binary.
std::string cli_path() {
    return (module_directory().parent_path().parent_path() / "Release" /
            "watashi_cli.exe")
        .string();
}

std::string ui_path() {
    return (module_directory().parent_path().parent_path() / "Release" /
            "watashi_ui.exe")
        .string();
}

/// Finds one of our child's top-level windows by class.
struct FindContext {
    DWORD process_id = 0;
    const wchar_t* class_name = nullptr;
    HWND found = nullptr;
};

BOOL CALLBACK find_window_proc(HWND hwnd, LPARAM parameter) {
    auto* context = reinterpret_cast<FindContext*>(parameter);
    DWORD process_id = 0;
    GetWindowThreadProcessId(hwnd, &process_id);
    if (process_id != context->process_id) {
        return TRUE;
    }
    wchar_t name[128]{};
    GetClassNameW(hwnd, name, 128);
    if (std::wstring(name) == std::wstring(context->class_name)) {
        context->found = hwnd;
        return FALSE;
    }
    return TRUE;
}

HWND find_child_window(DWORD process_id, const wchar_t* class_name) {
    FindContext context{process_id, class_name, nullptr};
    EnumWindows(&find_window_proc, reinterpret_cast<LPARAM>(&context));
    return context.found;
}

nlohmann::json read_json(const std::filesystem::path& path) {
    std::ifstream file(path, std::ios::binary);
    const std::string text(
        (std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    return nlohmann::json::parse(text);
}

/// A report file that cannot collide with a concurrent test and does not
/// outlive the case.
struct TempReport {
    TempReport() {
        std::random_device device;
        std::mt19937 generator(device());
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        path = module_directory() /
               ("watashi_report_" + std::to_string(stamp) + "_" +
                std::to_string(generator()) + ".json");
        std::filesystem::remove(path);
    }
    ~TempReport() { std::filesystem::remove(path); }

    std::filesystem::path path;
};

/// Serializes tests that put a real topmost overlay on the screen. ctest -j
/// otherwise runs the window test beside a report test, and two overlays or a
/// focus change from another process make the foreground/click-through
/// assertions meaningless.
class DesktopTestLock {
public:
    DesktopTestLock() {
        handle_ = CreateMutexW(nullptr, FALSE, L"watashi-desktop-test-lock");
        if (handle_ != nullptr) {
            WaitForSingleObject(handle_, INFINITE);
        }
    }
    ~DesktopTestLock() {
        if (handle_ != nullptr) {
            ReleaseMutex(handle_);
            CloseHandle(handle_);
        }
    }

private:
    HANDLE handle_ = nullptr;
};

/// Starts watashi_ui and waits for it to exit. Returns false when the UI could
/// not start at all.
bool run_ui(const std::vector<std::string>& arguments, int timeout_ms,
            int& exit_code) {
    bool exited = false;
    exit_code = -1;
    ui::ChildStream stream({ui_path(), arguments, "", 6});
    if (!stream.start(
            [](const std::string&) {},
            [&](int code, const std::string&) {
                exit_code = code;
                exited = true;
            })) {
        return false;
    }
    const bool ok = wait_until([&] { return exited; }, timeout_ms);
    stream.stop();
    return ok;
}

std::string envelope_line(
    const std::string& type,
    nlohmann::json data,
    std::int64_t seq) {
    const Envelope envelope = encode_event(type, std::move(data), seq, 1.0);
    return to_json_line(envelope);
}

}  // namespace

TEST_CASE("hotkeys are Ctrl+Alt+P, H and T with unique ids", "[ui][actions]") {
    const auto bindings = ui::hotkey_bindings();
    REQUIRE(bindings.size() == 3);
    REQUIRE(bindings[0].id == ui::kHotkeyPause);
    REQUIRE(bindings[1].id == ui::kHotkeyVisibility);
    REQUIRE(bindings[2].id == ui::kHotkeyLanguage);
    REQUIRE(bindings[0].virtual_key == 'P');
    REQUIRE(bindings[1].virtual_key == 'H');
    REQUIRE(bindings[2].virtual_key == 'T');
    for (const auto& binding : bindings) {
        REQUIRE(binding.modifiers == ui::kHotkeyModifiers);
    }
    // Control | Alt | No-repeat, as MOD_* would be spelled.
    REQUIRE(ui::kHotkeyModifiers == 0x4003u);
}

TEST_CASE("the tray menu reflects pause and visibility", "[ui][actions]") {
    const auto running = ui::tray_menu(false, true);
    REQUIRE(running.size() == 3);
    REQUIRE(running[0].id == ui::kTrayCommandPause);
    REQUIRE(running[0].label == "Pause");
    REQUIRE_FALSE(running[0].checked);
    REQUIRE(running[1].id == ui::kTrayCommandVisibility);
    REQUIRE(running[1].label == "Hide");
    REQUIRE(running[2].id == ui::kTrayCommandExit);
    REQUIRE(running[2].label == "Exit");

    const auto paused = ui::tray_menu(true, false);
    REQUIRE(paused[0].label == "Continue");
    REQUIRE(paused[0].checked);
    REQUIRE(paused[1].label == "Show");
    REQUIRE(paused[1].checked);
}

TEST_CASE("command payloads match the engine contract", "[ui][actions]") {
    REQUIRE(ui::pause_command(true).at("cmd") == "pause");
    REQUIRE(ui::pause_command(false).at("cmd") == "resume");

    const auto language = ui::target_language_command("en");
    REQUIRE(language.at("cmd") == "set_target_lang");
    REQUIRE(language.at("target_lang") == "en");

    REQUIRE(ui::shutdown_command().at("cmd") == "shutdown");
}

TEST_CASE("Ctrl+Alt+T alternates zh-CN and en", "[ui][actions]") {
    REQUIRE(ui::next_target_language("zh-CN") == "en");
    REQUIRE(ui::next_target_language("en") == "zh-CN");
    REQUIRE(ui::next_target_language("ja") == "en");
}

TEST_CASE("the model reads a command_result envelope", "[ui][model]") {
    const std::string line = envelope_line(
        "command_result",
        nlohmann::json{
            {"cmd", "pause"},
            {"ok", true},
            {"detail", "paused"},
        },
        7);
    const ui::StreamLine parsed = ui::parse_stream_line(line);
    REQUIRE(parsed.kind == ui::StreamLineKind::kCommandResult);
    REQUIRE(parsed.command.cmd == "pause");
    REQUIRE(parsed.command.ok);
    REQUIRE(parsed.command.detail == "paused");
    REQUIRE(parsed.seq == 7);
}

TEST_CASE("a command_result without ok is malformed", "[ui][model]") {
    const std::string line = envelope_line(
        "command_result",
        nlohmann::json{{"cmd", "pause"}, {"detail", "paused"}},
        1);
    const ui::StreamLine parsed = ui::parse_stream_line(line);
    REQUIRE(parsed.kind == ui::StreamLineKind::kMalformed);
}

TEST_CASE("the model keeps the last command and the current frame", "[ui][model]") {
    ui::UiModel model;
    model.ingest(envelope_line(
        "subtitle",
        nlohmann::json{{"source", "a"}, {"target", "A"}},
        1));
    model.ingest(envelope_line(
        "command_result",
        nlohmann::json{{"cmd", "pause"}, {"ok", true}, {"detail", "paused"}},
        2));

    const ui::UiSnapshot snapshot = model.snapshot();
    REQUIRE(snapshot.command_results == 1);
    REQUIRE(snapshot.last_command.cmd == "pause");
    REQUIRE(snapshot.last_command.ok);
    REQUIRE(snapshot.has_frame);
    REQUIRE(snapshot.frame.target == "A");
    REQUIRE(snapshot.last_seq == 2);
}

TEST_CASE("child stream writes a command line to the child stdin", "[ui][stream]") {
    std::mutex mutex;
    std::vector<std::string> lines;
    bool exited = false;

    ui::ChildStream stream(ui::ChildStream::Options{
        sibling("watashi_ui_stream_fixture.exe"),
        {"--count", "0", "--echo-stdin", "--echo-count", "2"},
        "",
        6});
    REQUIRE(stream.start(
        [&](const std::string& line) {
            std::lock_guard<std::mutex> lock(mutex);
            lines.push_back(line);
        },
        [&](int, const std::string&) {
            std::lock_guard<std::mutex> lock(mutex);
            exited = true;
        }));
    REQUIRE(stream.write_line("first"));
    REQUIRE(stream.write_line("second"));
    REQUIRE(wait_until([&] {
        std::lock_guard<std::mutex> lock(mutex);
        return exited;
    }, 5000));
    stream.stop();

    std::lock_guard<std::mutex> lock(mutex);
    REQUIRE(lines.size() == 2);
    REQUIRE(lines[0] == "echo: first");
    REQUIRE(lines[1] == "echo: second");
}

TEST_CASE("writing after stop fails instead of hanging", "[ui][stream]") {
    ui::ChildStream stream(ui::ChildStream::Options{
        sibling("watashi_ui_stream_fixture.exe"), {"--count", "0"}, "", 6});
    REQUIRE(stream.start([](const std::string&) {}, [](int, const std::string&) {}));
    stream.stop();
    REQUIRE_FALSE(stream.write_line("late"));
}

TEST_CASE("the CLI answers stdin commands and stops on shutdown", "[ui][cli]") {
    REQUIRE(std::filesystem::exists(cli_path()));

    std::mutex mutex;
    std::vector<Envelope> events;
    bool exited = false;
    int exit_code = -1;

    ui::ChildStream stream(ui::ChildStream::Options{
        cli_path(),
        {"--capture", "synthetic", "--ocr", "fake", "--translator", "fake",
         "--json-lines", "--command-stdin", "--duration", "1.0", "--fps", "20"},
        "",
        6});
    REQUIRE(stream.start(
        [&](const std::string& line) {
            const auto document = nlohmann::json::parse(line, nullptr, false);
            if (document.is_discarded()) {
                return;
            }
            try {
                const Envelope envelope = decode_envelope(document);
                std::lock_guard<std::mutex> lock(mutex);
                events.push_back(envelope);
            } catch (const ContractError&) {
            }
        },
        [&](int code, const std::string&) {
            std::lock_guard<std::mutex> lock(mutex);
            exit_code = code;
            exited = true;
        }));

    REQUIRE(stream.write_line(R"({"cmd":"status"})"));
    REQUIRE(stream.write_line(R"({"cmd":"pause"})"));
    REQUIRE(stream.write_line(R"({"cmd":"resume"})"));
    REQUIRE(stream.write_line(R"({"cmd":"shutdown"})"));
    REQUIRE(wait_until([&] {
        std::lock_guard<std::mutex> lock(mutex);
        return exited;
    }, 5000));
    stream.stop();

    std::lock_guard<std::mutex> lock(mutex);
    REQUIRE(exit_code == 0);
    REQUIRE_FALSE(events.empty());
    REQUIRE(events.front().type == "ready");
    REQUIRE(events.back().type == "stopped");
    REQUIRE(events.back().data.at("reason") == "shutdown");

    int results = 0;
    for (std::size_t index = 1; index < events.size(); ++index) {
        REQUIRE(events[index].seq == events[index - 1].seq + 1);
    }
    for (const auto& event : events) {
        if (event.type != "command_result") {
            continue;
        }
        ++results;
        REQUIRE(event.data.at("ok") == true);
    }
    REQUIRE(results == 4);
}

TEST_CASE("tray menu ids map to the three actions", "[ui][actions]") {
    REQUIRE(static_cast<int>(ui::tray_action(ui::kTrayCommandPause)) ==
            static_cast<int>(ui::TrayAction::kTogglePause));
    REQUIRE(static_cast<int>(ui::tray_action(ui::kTrayCommandVisibility)) ==
            static_cast<int>(ui::TrayAction::kToggleVisibility));
    REQUIRE(static_cast<int>(ui::tray_action(ui::kTrayCommandExit)) ==
            static_cast<int>(ui::TrayAction::kExit));
    REQUIRE(static_cast<int>(ui::tray_action(0)) ==
            static_cast<int>(ui::TrayAction::kNone));
    REQUIRE(static_cast<int>(ui::tray_action(1004)) ==
            static_cast<int>(ui::TrayAction::kNone));
    REQUIRE(static_cast<int>(ui::tray_action(-1)) ==
            static_cast<int>(ui::TrayAction::kNone));
}

TEST_CASE("the tray menu follows every pause and visibility state", "[ui][actions]") {
    for (int state = 0; state < 4; ++state) {
        const bool paused = (state & 1) != 0;
        const bool visible = (state & 2) != 0;
        const auto menu = ui::tray_menu(paused, visible);
        REQUIRE(menu.size() == 3);
        REQUIRE(menu[0].id == ui::kTrayCommandPause);
        REQUIRE(menu[0].label == (paused ? "Continue" : "Pause"));
        REQUIRE(menu[0].checked == paused);
        REQUIRE(menu[1].id == ui::kTrayCommandVisibility);
        REQUIRE(menu[1].label == (visible ? "Hide" : "Show"));
        REQUIRE(menu[1].checked == !visible);
        REQUIRE(menu[2].id == ui::kTrayCommandExit);
        REQUIRE(menu[2].label == "Exit");
        REQUIRE_FALSE(menu[2].checked);
        REQUIRE(static_cast<int>(ui::tray_action(menu[0].id)) ==
                static_cast<int>(ui::TrayAction::kTogglePause));
        REQUIRE(static_cast<int>(ui::tray_action(menu[1].id)) ==
                static_cast<int>(ui::TrayAction::kToggleVisibility));
        REQUIRE(static_cast<int>(ui::tray_action(menu[2].id)) ==
                static_cast<int>(ui::TrayAction::kExit));
    }
}

TEST_CASE("the tray owner is a different, activatable window", "[ui][tray]") {
    ui::LayeredWindow overlay;
    if (!overlay.create()) {
        SKIP("no desktop session");
    }
    ui::TrayIcon tray;
    REQUIRE(tray.hwnd() == nullptr);
    tray.add();
    REQUIRE(tray.hwnd() != nullptr);
    REQUIRE(tray.hwnd() != overlay.hwnd());

    // The menu's owner must be able to become foreground; the overlay must
    // keep its NOACTIVATE promise.
    const DWORD tray_style =
        static_cast<DWORD>(GetWindowLongPtrW(tray.hwnd(), GWL_EXSTYLE));
    const DWORD overlay_style =
        static_cast<DWORD>(GetWindowLongPtrW(overlay.hwnd(), GWL_EXSTYLE));
    REQUIRE((tray_style & WS_EX_TOOLWINDOW) != 0);
    REQUIRE((tray_style & WS_EX_NOACTIVATE) == 0);
    REQUIRE((tray_style & WS_EX_TRANSPARENT) == 0);
    REQUIRE((overlay_style & WS_EX_NOACTIVATE) != 0);
    REQUIRE((overlay_style & WS_EX_TRANSPARENT) != 0);

    wchar_t class_name[128]{};
    GetClassNameW(tray.hwnd(), class_name, 128);
    REQUIRE(std::wstring(class_name) == std::wstring(ui::kTrayWindowClass));
    REQUIRE(IsWindowVisible(tray.hwnd()));

    tray.remove();
    REQUIRE(tray.hwnd() == nullptr);
}

TEST_CASE("tray.add reports what the shell accepted", "[ui][tray]") {
    ui::TrayIcon tray;
    const bool added = tray.add();
    REQUIRE(tray.hwnd() != nullptr);
    // The owner exists whether or not a notification area accepted the icon,
    // so the report's tray.added is the shell's own answer.
    REQUIRE(tray.present() == added);
    tray.remove();
    REQUIRE_FALSE(tray.present());
    REQUIRE(tray.hwnd() == nullptr);
}

TEST_CASE("a duration run writes its report", "[ui][report]") {
    REQUIRE(std::filesystem::exists(ui_path()));
    DesktopTestLock desktop;
    TempReport report;
    int exit_code = -1;
    const bool exited = run_ui(
        {"--capture", "synthetic", "--ocr", "fake", "--translator", "fake",
         "--duration", "1", "--linger", "0", "--report", report.path.string()},
        15000, exit_code);
    if (!exited || exit_code == 3) {
        SKIP("no desktop session");
    }
    REQUIRE(exit_code == 0);
    REQUIRE(std::filesystem::exists(report.path));

    const nlohmann::json document = read_json(report.path);
    REQUIRE(document.at("exit_reason") == "duration");
    REQUIRE(document.at("exit_code") == 0);
    REQUIRE(document.at("hotkeys").at("attempted") == 3);
    // Registered and failed are the startup measurements, kept after the
    // cleanup that unregisters every hotkey.
    const int registered = document.at("hotkeys").at("registered").get<int>();
    const int failed = document.at("hotkeys").at("failed").get<int>();
    REQUIRE(registered + failed == 3);
    REQUIRE(document.at("hotkeys").at("all_registered") == (registered == 3));
    REQUIRE(document.at("tray").at("added").is_boolean());
    REQUIRE(document.at("click_through").at("passes").is_boolean());
}

TEST_CASE("WM_CLOSE writes the report as well", "[ui][report]") {
    REQUIRE(std::filesystem::exists(ui_path()));
    DesktopTestLock desktop;
    TempReport report;
    bool exited = false;
    int exit_code = -1;
    ui::ChildStream stream({ui_path(),
                            {"--capture", "synthetic", "--ocr", "fake",
                             "--translator", "fake", "--duration", "30",
                             "--linger", "0", "--report", report.path.string()},
                            "", 6});
    REQUIRE(stream.start(
        [](const std::string&) {},
        [&](int code, const std::string&) {
            exit_code = code;
            exited = true;
        }));

    HWND overlay = nullptr;
    const bool appeared = wait_until(
        [&] {
            overlay = find_child_window(stream.process_id(), ui::kOverlayWindowClass);
            return overlay != nullptr;
        },
        5000);
    if (!appeared) {
        stream.stop();
        SKIP("no desktop session");
    }
    PostMessageW(overlay, WM_CLOSE, 0, 0);
    REQUIRE(wait_until([&] { return exited; }, 10000));
    stream.stop();
    REQUIRE(exit_code == 0);
    REQUIRE(std::filesystem::exists(report.path));
    REQUIRE(read_json(report.path).at("exit_reason") == "wm-close");
}
