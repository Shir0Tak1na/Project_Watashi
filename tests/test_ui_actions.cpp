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
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

#include "watashi/commands.hpp"
#include "watashi/events.hpp"
#include "watashi/ui/actions.hpp"
#include "watashi/ui/child_stream.hpp"
#include "watashi/ui/model.hpp"

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

/// The CLI is built in build-*/Release, two directories above the test binary.
std::string cli_path() {
    return (module_directory().parent_path().parent_path() / "Release" /
            "watashi_cli.exe")
        .string();
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
