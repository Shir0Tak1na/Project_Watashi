// watashi_ui
//
// The native overlay: spawn the engine, read its NDJSON stream, paint the
// latest subtitle in a borderless click-through window. It owns no OCR, no
// corpus and no model -- if a subtitle is wrong, the bug is upstream by
// construction.
//
//   watashi_ui --capture window --window-title "..." --ocr onnx \
//              --translator corpus --corpus domain=rules/fiction \
//              --det ... --rec ... --chars ... --duration 300 \
//              --report build/w3_report.json
//
// Exit codes: 0 the stream ended normally, 1 the engine failed or could not be
// started, 2 bad arguments, 3 no desktop session (window creation failed).
// Exit 3 is the honest answer on a machine without a compositor; no window was
// shown, so no click-through or DPI claim may be made.

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <psapi.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "obs/obs.hpp"
#include "watashi/capture.hpp"
#include "watashi/ui/actions.hpp"
#include "watashi/ui/child_stream.hpp"
#include "watashi/ui/layered_window.hpp"
#include "watashi/ui/layout.hpp"
#include "watashi/ui/model.hpp"
#include "watashi/ui/renderer.hpp"
#include "watashi/ui/settings.hpp"
#include "watashi/ui/settings_window.hpp"
#include "watashi/ui/tray.hpp"

namespace {

using namespace watashi;
using namespace watashi::ui;

constexpr UINT kTimerIntervalMs = 50;
constexpr double kStaleAfterSeconds = 3.0;
constexpr double kMemorySampleSeconds = 5.0;
constexpr double kCommandNoteSeconds = 3.0;

struct Args {
    /// Empty means "watashi_cli.exe next to watashi_ui.exe". A hard-coded build
    /// directory would point at a stale engine on a clean checkout.
    std::string cli;

    std::string capture = "window";
    std::string ocr = "onnx";
    std::string translator = "corpus";
    std::string window_title;
    std::string hwnd;
    std::string region;
    std::string fps;
    std::string duration;
    std::string diff_threshold;
    std::vector<std::string> corpus;
    std::vector<std::string> rules;
    std::string target_lang;
    std::string scene;
    std::string window;
    std::string det;
    std::string rec;
    std::string cls;
    std::string chars;

    std::string preview_dir;
    std::string report_path;
    std::string final_image;
    /// Empty means config/presentation.json when it exists, else the built-in
    /// bar preset.
    std::string presentation;
    double linger = 8.0;

    /// Open the generated settings window alongside the overlay.
    bool settings = false;
    /// Print the version stamp and exit; no window, no engine.
    bool version = false;
    /// User settings file; empty means config/user.json.
    std::string config_path;
};

void usage(std::ostream& out) {
    out << "usage: watashi_ui [viewer options] --capture MODE [engine options]\n"
           "\n"
           "viewer:\n"
           "  --cli PATH             engine binary, default watashi_cli.exe beside\n"
           "                         watashi_ui.exe (no build directory is assumed)\n"
           "  --linger SECONDS       keep the last frame after the stream ends, default 8\n"
           "  --report PATH          write a JSON report of this run\n"
           "  --final-image PATH     write the last composed frame as a BMP\n"
           "  --presentation PATH    presentation spec; default config/presentation.json\n"
           "                         when it exists, else the built-in bar preset\n"
           "  --preview DIR          render 96/120/144 dpi proofs and exit (no window)\n"
           "  --settings             open the settings window generated from\n"
           "                         config/settings_ui.json\n"
           "  --version              print product version, git commit and build type\n"
           "  --config PATH          user settings file, default config/user.json\n"
           "\n"
           "engine (passed through):\n"
           "  --capture screen|window|synthetic\n"
           "  --window-title TEXT  --hwnd N  --region x,y,w,h\n"
           "  --fps N  --duration S  --diff-threshold 0..1\n"
           "  --ocr onnx|fake\n"
           "  --translator corpus|nmt|hybrid|fake\n"
           "  --corpus LAYER=PATH  --rules PATH  --target LANG  --scene NAME\n"
           "  --det PATH  --rec PATH  --cls PATH  --chars PATH\n";
}

bool take_value(int& index, int argc, char** argv, std::string& value) {
    if (index + 1 >= argc) {
        std::cerr << "missing value after " << argv[index] << "\n";
        return false;
    }
    value = argv[++index];
    return true;
}

bool parse_args(int argc, char** argv, Args& args) {
    for (int index = 1; index < argc; ++index) {
        const std::string argument = argv[index];
        std::string value;
        if (argument == "--cli") {
            if (!take_value(index, argc, argv, args.cli)) return false;
        } else if (argument == "--capture") {
            if (!take_value(index, argc, argv, args.capture)) return false;
        } else if (argument == "--ocr") {
            if (!take_value(index, argc, argv, args.ocr)) return false;
        } else if (argument == "--translator") {
            if (!take_value(index, argc, argv, args.translator)) return false;
        } else if (argument == "--window-title") {
            if (!take_value(index, argc, argv, args.window_title)) return false;
        } else if (argument == "--hwnd") {
            if (!take_value(index, argc, argv, args.hwnd)) return false;
        } else if (argument == "--region") {
            if (!take_value(index, argc, argv, args.region)) return false;
        } else if (argument == "--fps") {
            if (!take_value(index, argc, argv, args.fps)) return false;
        } else if (argument == "--duration") {
            if (!take_value(index, argc, argv, args.duration)) return false;
        } else if (argument == "--diff-threshold") {
            if (!take_value(index, argc, argv, args.diff_threshold)) return false;
        } else if (argument == "--corpus") {
            if (!take_value(index, argc, argv, value)) return false;
            args.corpus.push_back(value);
        } else if (argument == "--rules") {
            if (!take_value(index, argc, argv, value)) return false;
            args.rules.push_back(value);
        } else if (argument == "--target") {
            if (!take_value(index, argc, argv, args.target_lang)) return false;
        } else if (argument == "--scene") {
            if (!take_value(index, argc, argv, args.scene)) return false;
        } else if (argument == "--window") {
            if (!take_value(index, argc, argv, args.window)) return false;
        } else if (argument == "--det") {
            if (!take_value(index, argc, argv, args.det)) return false;
        } else if (argument == "--rec") {
            if (!take_value(index, argc, argv, args.rec)) return false;
        } else if (argument == "--cls") {
            if (!take_value(index, argc, argv, args.cls)) return false;
        } else if (argument == "--chars") {
            if (!take_value(index, argc, argv, args.chars)) return false;
        } else if (argument == "--presentation") {
            if (!take_value(index, argc, argv, args.presentation)) return false;
        } else if (argument == "--preview") {
            if (!take_value(index, argc, argv, args.preview_dir)) return false;
        } else if (argument == "--report") {
            if (!take_value(index, argc, argv, args.report_path)) return false;
        } else if (argument == "--final-image") {
            if (!take_value(index, argc, argv, args.final_image)) return false;
        } else if (argument == "--linger") {
            if (!take_value(index, argc, argv, value)) return false;
            args.linger = std::atof(value.c_str());
            if (args.linger < 0.0) {
                std::cerr << "--linger must be >= 0\n";
                return false;
            }
        } else if (argument == "--settings") {
            args.settings = true;
        } else if (argument == "--version") {
            args.version = true;
        } else if (argument == "--config") {
            if (!take_value(index, argc, argv, args.config_path)) return false;
        } else if (argument == "--help" || argument == "-h") {
            usage(std::cout);
            std::exit(0);
        } else {
            std::cerr << "unknown argument: " << argument << "\n";
            usage(std::cerr);
            return false;
        }
    }
    return true;
}

bool validate_args(const Args& args) {
    if (args.capture != "screen" && args.capture != "window" &&
        args.capture != "synthetic") {
        std::cerr << "--capture must be screen, window or synthetic\n";
        return false;
    }
    if (args.capture == "window" && args.hwnd.empty() && args.window_title.empty()) {
        std::cerr << "--capture window needs --hwnd or --window-title\n";
        return false;
    }
    return true;
}

std::vector<std::string> engine_arguments(const Args& args) {
    std::vector<std::string> arguments = {
        "--capture", args.capture,
        "--ocr", args.ocr,
        "--translator", args.translator,
        "--json-lines",
        // Hotkeys and the tray send JSON commands down this pipe; without the
        // flag the engine never reads stdin and every command would be lost.
        "--command-stdin",
    };
    auto add = [&arguments](const std::string& flag, const std::string& value) {
        if (!value.empty()) {
            arguments.push_back(flag);
            arguments.push_back(value);
        }
    };
    add("--fps", args.fps);
    add("--duration", args.duration);
    add("--diff-threshold", args.diff_threshold);
    add("--region", args.region);
    add("--hwnd", args.hwnd);
    add("--window-title", args.window_title);
    // The corpus uses the window title for when_window conditions; the user
    // may override it explicitly with --window.
    add("--window", args.window.empty() ? args.window_title : args.window);
    add("--target", args.target_lang);
    add("--scene", args.scene);
    for (const auto& spec : args.corpus) {
        arguments.push_back("--corpus");
        arguments.push_back(spec);
    }
    for (const auto& path : args.rules) {
        arguments.push_back("--rules");
        arguments.push_back(path);
    }
    if (args.ocr == "onnx") {
        add("--det", args.det);
        add("--rec", args.rec);
        add("--cls", args.cls);
        add("--chars", args.chars);
    }
    return arguments;
}

/// Full path of another executable shipped beside this binary. The engine is
/// always built next to the UI, so this works in any build tree without the UI
/// knowing the tree's name.
std::string sibling_executable(const std::string& name) {
    wchar_t module[32768]{};
    if (GetModuleFileNameW(nullptr, module, 32768) == 0) {
        return name;
    }
    std::filesystem::path path(module);
    path.replace_filename(std::wstring(name.begin(), name.end()));
    return path.u8string();
}

CaptureRect primary_work_area() {
    MONITORINFO info{};
    info.cbSize = sizeof(info);
    HMONITOR monitor = MonitorFromPoint(POINT{0, 0}, MONITOR_DEFAULTTOPRIMARY);
    if (monitor != nullptr && GetMonitorInfoW(monitor, &info)) {
        CaptureRect rect;
        rect.x = info.rcWork.left;
        rect.y = info.rcWork.top;
        rect.width = info.rcWork.right - info.rcWork.left;
        rect.height = info.rcWork.bottom - info.rcWork.top;
        return rect;
    }
    CaptureRect fallback;
    fallback.width = GetSystemMetrics(SM_CXSCREEN);
    fallback.height = GetSystemMetrics(SM_CYSCREEN);
    return fallback;
}

int window_dpi(HWND hwnd) {
    if (hwnd != nullptr) {
        const int dpi = GetDpiForWindow(hwnd);
        if (dpi > 0) {
            return dpi;
        }
    }
    HDC dc = GetDC(nullptr);
    const int dpi = GetDeviceCaps(dc, LOGPIXELSX);
    ReleaseDC(nullptr, dc);
    return dpi > 0 ? dpi : 96;
}

double working_set_mib() {
    PROCESS_MEMORY_COUNTERS counters{};
    if (!GetProcessMemoryInfo(GetCurrentProcess(), &counters, sizeof(counters))) {
        return 0.0;
    }
    return static_cast<double>(counters.WorkingSetSize) / (1024.0 * 1024.0);
}

nlohmann::json rect_json(const CaptureRect& rect) {
    return nlohmann::json{
        {"x", rect.x},
        {"y", rect.y},
        {"width", rect.width},
        {"height", rect.height},
    };
}

struct PreviewEntry {
    int dpi = 96;
    CaptureRect overlay;
    std::string bitmap;
};

std::string sample_source() {
    return "the sword intent of this sect is a myth\n"
           "he broke through to the void realm";
}

std::string sample_target() {
    return "本门剑意只是传说\n"
           "他突破到了虚空境界";
}

/// Explicit --presentation wins; otherwise the shipped config file when it
/// exists; otherwise the built-in bar preset (empty path).
std::string resolve_presentation_path(const Args& args, bool& explicit_path) {
    if (!args.presentation.empty()) {
        explicit_path = true;
        return args.presentation;
    }
    explicit_path = false;
    const std::string default_path = "config/presentation.json";
    std::error_code error;
    if (std::filesystem::exists(default_path, error)) {
        return default_path;
    }
    return std::string();
}

int run_preview(const Args& args) {
    bool explicit_path = false;
    const std::string spec_path = resolve_presentation_path(args, explicit_path);
    if (explicit_path) {
        std::error_code error;
        if (!std::filesystem::exists(spec_path, error)) {
            std::cerr << "watashi_ui: presentation file not found: " << spec_path
                      << "\n";
            return 2;
        }
    }

    const CaptureRect work_area = primary_work_area();
    if (work_area.width <= 0 || work_area.height <= 0) {
        std::cerr << "watashi_ui: no desktop session: no monitor reports a work area\n";
        return 3;
    }

    PresentationWatcher presentation(spec_path);
    if (!presentation.error().empty()) {
        std::cerr << "presentation: " << presentation.error()
                  << "; using the built-in bar preset\n";
    }

    std::filesystem::create_directories(args.preview_dir);
    GdiText text;
    const TextMeasure measure = text.as_measure();
    std::vector<PreviewEntry> entries;

    for (int dpi : {96, 120, 144}) {
        LayoutRequest request;
        request.text.source = sample_source();
        request.text.target = sample_target();
        request.text.status = "preview · dpi=" + std::to_string(dpi);
        request.work_area = work_area;
        request.dpi = dpi;
        request.spec = presentation.spec();

        const Layout layout = compute_layout(request, measure);
        const OverlayImage image = compose_overlay(layout, text);
        const std::string path =
            (std::filesystem::path(args.preview_dir) /
             ("overlay_" + std::to_string(dpi) + ".bmp"))
                .string();
        if (!write_bmp(path, image)) {
            std::cerr << "watashi_ui: cannot write " << path << "\n";
            return 1;
        }
        PreviewEntry entry;
        entry.dpi = dpi;
        entry.overlay = CaptureRect{layout.x, layout.y, layout.width, layout.height};
        entry.bitmap = std::filesystem::path(path).filename().string();
        entries.push_back(entry);
        std::cerr << "preview " << dpi << " dpi: overlay " << layout.width << "x"
                  << layout.height << "+" << layout.x << "+" << layout.y << " -> "
                  << path << "\n";
    }

    nlohmann::json payload;
    payload["kind"] = "watashi-ui-preview";
    payload["work_area"] = rect_json(work_area);
    payload["source"] = sample_source();
    payload["target"] = sample_target();
    payload["presentation"] = nlohmann::json{
        {"path", spec_path},
        {"name", presentation.spec().name},
        {"revision", presentation.revision()},
        {"error", presentation.error()},
    };
    payload["entries"] = nlohmann::json::array();
    for (const auto& entry : entries) {
        payload["entries"].push_back(nlohmann::json{
            {"dpi", entry.dpi},
            {"scale", dpi_scale(entry.dpi)},
            {"overlay", rect_json(entry.overlay)},
            {"bitmap", entry.bitmap},
        });
    }
    const std::string layout_path =
        (std::filesystem::path(args.preview_dir) / "layout.json").string();
    std::ofstream file(layout_path, std::ios::binary | std::ios::trunc);
    if (!file) {
        std::cerr << "watashi_ui: cannot write " << layout_path << "\n";
        return 1;
    }
    file << payload.dump(2) << '\n';
    std::cerr << "layout: " << layout_path << "\n";
    return 0;
}

std::string status_text(
    const UiSnapshot& snapshot,
    const std::string& launch_error,
    const std::string& capture_mode,
    bool paused,
    double now) {
    if (!launch_error.empty()) {
        return "cannot start engine: " + launch_error;
    }
    if (snapshot.stream_ended) {
        std::string text =
            "stream ended (exit " + std::to_string(snapshot.exit_code) + ")";
        if (!snapshot.stderr_tail.empty()) {
            text += " · " + snapshot.stderr_tail;
        }
        return text;
    }
    std::string text =
        std::string(paused ? "paused" : "live") + " · " + capture_mode + " · " +
        std::to_string(snapshot.subtitles) + " frame(s)";
    if (snapshot.malformed > 0) {
        text += " · " + std::to_string(snapshot.malformed) + " bad line(s)";
    }
    if (snapshot.missing_records > 0) {
        text += " · " + std::to_string(snapshot.missing_records) + " missing";
    }
    if (snapshot.subtitles == 0) {
        text += " · waiting for subtitles";
    }
    if (!paused) {
        const double quiet = now - snapshot.last_line_seconds;
        if (quiet > kStaleAfterSeconds) {
            text += " · no data for " + std::to_string(static_cast<int>(quiet)) + "s";
        }
    }
    if (!snapshot.last_command.cmd.empty() &&
        now - snapshot.last_command_seconds <= kCommandNoteSeconds) {
        text += " · cmd " + snapshot.last_command.cmd +
                (snapshot.last_command.ok ? " ok" : " failed");
    }
    return text;
}

struct MemorySample {
    double first = 0.0;
    double last = 0.0;
    double maximum = 0.0;
};

struct Run {
    UiModel model;
    LayeredWindow window;
    ChildStream* stream = nullptr;
    GdiText text;
    CaptureRect work_area;
    int dpi = 96;
    std::string capture_mode;
    PresentationWatcher presentation;

    std::uint64_t rendered_version = 0;
    std::uint64_t rendered_revision = 0;
    bool has_version = false;
    std::uint64_t has_revision = 0;
    std::string rendered_status;
    int rendered_dpi = 0;
    bool presented = false;
    Layout layout;
    OverlayImage image;

    std::string launch_error;
    double started = 0.0;
    double linger = 8.0;
    double ended_at = -1.0;
    bool quit_requested = false;
    /// Why the loop ended: duration, tray-exit, wm-close, console or exception.
    /// Written to the report so a reader knows which path produced it.
    std::string finish_reason;

    /// Engine pause requested through a hotkey or the tray. The engine itself
    /// is paused by the ``pause`` command; this flag only drives the status
    /// line and the menu label.
    bool paused = false;
    /// UI-only visibility, independent of the engine and of ``mode: hidden``.
    bool hidden_by_user = false;
    bool visibility_dirty = false;
    std::string target_lang = "zh-CN";
    TrayIcon tray;
    /// Measured once at startup and kept: the tray is removed before the report
    /// is written, so reading the icon back then would always answer false.
    bool tray_added = false;
    int hotkey_successes = 0;
    int hotkey_failures = 0;

    bool click_checked = false;
    HWND foreground_before = nullptr;
    HWND foreground_after = nullptr;
    HWND window_under_center = nullptr;

    double last_memory_sample = 0.0;
    MemorySample memory;
};

void schedule_quit_if_done(
    Run& run,
    const UiSnapshot& snapshot,
    double now,
    bool settings_open) {
    if (!snapshot.stream_ended) {
        return;
    }
    if (run.ended_at < 0.0) {
        run.ended_at = now;
    }
    // The settings window is an explicit surface: keep it open after the stream
    // ends so values can still be edited and saved. Closing it resumes the
    // normal exit, already due if the linger elapsed.
    if (settings_open) {
        return;
    }
    // Always paint the final status at least once before closing.
    if (run.presented && now - run.ended_at >= run.linger) {
        run.finish_reason = "duration";
        run.quit_requested = true;
        PostQuitMessage(0);
    }
}

void render_if_dirty(Run& run, const UiSnapshot& snapshot, double now) {
    std::string status = status_text(
        snapshot, run.launch_error, run.capture_mode, run.paused, now);
    const std::string& presentation_error = run.presentation.error();
    if (!presentation_error.empty()) {
        status += " · presentation: " + presentation_error;
    }

    const bool dirty = !run.has_version ||
                       snapshot.version != run.rendered_version ||
                       status != run.rendered_status ||
                       run.dpi != run.rendered_dpi ||
                       run.presentation.revision() != run.rendered_revision ||
                       run.visibility_dirty;
    if (!dirty) {
        return;
    }

    LayoutRequest request;
    // The overlay publishes the frame's own source/target/trace text. ``lines``
    // and boxes are deliberately not used yet: the native surface draws one
    // bar, not the per-line geometry modes.
    request.text.source = snapshot.frame.source;
    request.text.target = snapshot.frame.target;
    request.text.trace = snapshot.frame.trace;
    request.text.previous = snapshot.previous_target;
    request.text.status = status;
    request.text.confidence = snapshot.frame.confidence;
    request.text.coverage = snapshot.frame.coverage;
    request.work_area = run.work_area;
    request.dpi = run.dpi;
    request.spec = run.presentation.spec();

    run.layout = compute_layout(request, run.text.as_measure());
    run.image = compose_overlay(run.layout, run.text);
    if (run.hidden_by_user || run.layout.hidden || run.image.empty()) {
        run.window.set_visible(false);
    } else {
        run.window.present(run.image, run.layout.x, run.layout.y);
        run.window.set_visible(true);
    }
    run.visibility_dirty = false;

    run.has_version = true;
    run.rendered_version = snapshot.version;
    run.rendered_revision = run.presentation.revision();
    run.rendered_status = status;
    run.rendered_dpi = run.dpi;
    run.presented = true;

    if (!run.click_checked && !run.layout.empty && !run.layout.hidden &&
        !run.hidden_by_user) {
        run.click_checked = true;
        POINT center{
            run.layout.x + run.layout.width / 2,
            run.layout.y + run.layout.height / 2,
        };
        run.window_under_center = WindowFromPoint(center);
        run.foreground_after = GetForegroundWindow();
    }
}

bool send_command(Run& run, const nlohmann::json& command) {
    if (run.stream == nullptr) {
        std::cerr << "command: " << command.dump() << " (engine not running)\n";
        return false;
    }
    const std::string line = command.dump();
    const bool sent = run.stream->write_line(line);
    std::cerr << "command: " << line << (sent ? "" : " (write failed)") << "\n";
    return sent;
}

void toggle_pause(Run& run) {
    run.paused = !run.paused;
    send_command(run, pause_command(run.paused));
    // The status line switches between live and paused immediately.
    run.visibility_dirty = true;
}

void toggle_visibility(Run& run) {
    run.hidden_by_user = !run.hidden_by_user;
    run.visibility_dirty = true;
    if (run.hidden_by_user) {
        run.window.set_visible(false);
    } else {
        // Re-measure click-through after the window comes back.
        run.click_checked = false;
    }
}

void toggle_language(Run& run) {
    run.target_lang = next_target_language(run.target_lang);
    send_command(run, target_language_command(run.target_lang));
    run.visibility_dirty = true;
}

void handle_hotkey(Run& run, int id) {
    switch (id) {
        case kHotkeyPause:
            toggle_pause(run);
            break;
        case kHotkeyVisibility:
            toggle_visibility(run);
            break;
        case kHotkeyLanguage:
            toggle_language(run);
            break;
        default:
            break;
    }
}

void handle_tray_command(Run& run, int id) {
    switch (tray_action(id)) {
        case TrayAction::kTogglePause:
            toggle_pause(run);
            break;
        case TrayAction::kToggleVisibility:
            toggle_visibility(run);
            break;
        case TrayAction::kExit:
            run.finish_reason = "tray-exit";
            send_command(run, shutdown_command());
            run.quit_requested = true;
            PostQuitMessage(0);
            break;
        case TrayAction::kNone:
            break;
    }
}

void register_hotkeys(Run& run) {
    for (const auto& binding : hotkey_bindings()) {
        if (RegisterHotKey(
                run.window.hwnd(), binding.id, binding.modifiers, binding.virtual_key)) {
            ++run.hotkey_successes;
        } else {
            ++run.hotkey_failures;
            std::cerr << "hotkey: Ctrl+Alt+key " << binding.virtual_key
                      << " registration failed (" << GetLastError() << ")\n";
        }
    }
}

void unregister_hotkeys(Run& run) {
    if (run.window.hwnd() == nullptr) {
        return;
    }
    for (const auto& binding : hotkey_bindings()) {
        UnregisterHotKey(run.window.hwnd(), binding.id);
    }
}

void sample_memory(Run& run, double now) {
    if (now - run.last_memory_sample < kMemorySampleSeconds) {
        return;
    }
    run.last_memory_sample = now;
    const double mib = working_set_mib();
    if (run.memory.first == 0.0) {
        run.memory.first = mib;
    }
    run.memory.last = mib;
    run.memory.maximum = std::max(run.memory.maximum, mib);
}

void write_report(
    const Args& args,
    const std::string& cli,
    const std::vector<std::string>& engine_args,
    const Run& run,
    const UiSnapshot& snapshot,
    int exit_code,
    const std::string& finish_reason) {
    if (args.report_path.empty()) {
        return;
    }
    nlohmann::json payload;
    payload["kind"] = "watashi-ui-run";
    const watashi::obs::VersionInfo build = watashi::obs::version();
    payload["product"] = nlohmann::json{
        {"name", build.product},
        {"version", build.version},
        {"commit", build.commit},
        {"build_type", build.build_type},
    };
    payload["cli"] = cli;
    payload["arguments"] = engine_args;
    payload["dpi"] = run.dpi;
    payload["work_area"] = rect_json(run.work_area);
    payload["overlay"] = rect_json(
        CaptureRect{run.layout.x, run.layout.y, run.layout.width, run.layout.height});
    payload["rendered"] = run.presented;
    payload["click_through"] = nlohmann::json{
        {"overlay_hwnd", reinterpret_cast<std::uintptr_t>(run.window.hwnd())},
        {"window_under_center", reinterpret_cast<std::uintptr_t>(run.window_under_center)},
        {"passes", run.click_checked &&
                       run.window_under_center != nullptr &&
                       run.window_under_center != run.window.hwnd()},
    };
    payload["foreground"] = nlohmann::json{
        {"before", reinterpret_cast<std::uintptr_t>(run.foreground_before)},
        {"after", reinterpret_cast<std::uintptr_t>(run.foreground_after)},
        {"stable", run.foreground_after == run.foreground_before},
    };
    payload["stream"] = nlohmann::json{
        {"subtitles", snapshot.subtitles},
        {"malformed", snapshot.malformed},
        {"ignored", snapshot.ignored},
        {"out_of_order", snapshot.out_of_order},
        {"missing_records", snapshot.missing_records},
        {"exit_code", snapshot.exit_code},
        {"launch_failed", snapshot.launch_failed},
        {"last_seq", snapshot.last_seq},
        {"stderr_tail", snapshot.stderr_tail},
        {"last_error", snapshot.last_error},
        {"has_frame", snapshot.has_frame},
        {"source", snapshot.frame.source},
        {"target", snapshot.frame.target},
        {"backend", snapshot.frame.backend},
    };
    payload["memory_mib"] = nlohmann::json{
        {"first", run.memory.first},
        {"last", run.memory.last},
        {"max", run.memory.maximum},
    };
    payload["presentation"] = nlohmann::json{
        {"path", run.presentation.path()},
        {"name", run.presentation.spec().name},
        {"revision", run.presentation.revision()},
        {"error", run.presentation.error()},
    };
    payload["hotkeys"] = nlohmann::json{
        {"attempted", 3},
        {"registered", run.hotkey_successes},
        {"failed", run.hotkey_failures},
        {"all_registered", run.hotkey_successes == 3},
    };
    payload["tray"] = nlohmann::json{{"added", run.tray_added}};
    payload["commands"] = nlohmann::json{
        {"paused", run.paused},
        {"hidden", run.hidden_by_user},
        {"target_lang", run.target_lang},
        {"results", snapshot.command_results},
        {"last_cmd", snapshot.last_command.cmd},
        {"last_ok", snapshot.last_command.ok},
    };
    payload["exit_code"] = exit_code;
    payload["exit_reason"] = finish_reason.empty() ? "duration" : finish_reason;

    std::filesystem::path path(args.report_path);
    if (!path.parent_path().empty()) {
        std::filesystem::create_directories(path.parent_path());
    }
    std::ofstream file(path, std::ios::binary | std::ios::trunc);
    if (!file) {
        std::cerr << "watashi_ui: cannot write report " << path.string() << "\n";
        return;
    }
    file << payload.dump(2) << '\n';
    std::cerr << "report: " << path.string() << "\n";
}

// The console handler cannot touch the UI: DestroyWindow, the child pipe and
// the model belong to the main thread. It asks the loop to finish (WM_CLOSE)
// and waits for finish() to write the report. If the loop is stuck -- for
// example inside a modal popup menu -- the wait times out and the process is
// killed by the console subsystem without a report; a forced kill can never
// write one, and this code does not pretend otherwise.
std::atomic<HWND> g_console_window{nullptr};
std::atomic<bool> g_console_requested{false};
std::atomic<bool> g_console_finished{false};

BOOL WINAPI console_control_handler(DWORD type) {
    if (type != CTRL_C_EVENT && type != CTRL_BREAK_EVENT && type != CTRL_CLOSE_EVENT) {
        return FALSE;
    }
    g_console_requested.store(true);
    HWND overlay = g_console_window.load();
    if (overlay != nullptr) {
        PostMessageW(overlay, WM_CLOSE, 0, 0);
    }
    const DWORD deadline = GetTickCount() + 4000;
    while (!g_console_finished.load() && GetTickCount() < deadline) {
        Sleep(20);
    }
    return TRUE;
}

int run_overlay(const Args& args) {
    const std::string cli =
        args.cli.empty() ? sibling_executable("watashi_cli.exe") : args.cli;
    if (!std::filesystem::exists(cli)) {
        std::cerr << "watashi_ui: engine binary not found: " << cli
                  << " (build it or pass --cli PATH)\n";
        return 2;
    }

    // The generated settings document and the user's overrides. The window
    // builds its controls from this; nothing here lists a field by name.
    const std::string settings_document = "config/settings_ui.json";
    std::string schema_error;
    SettingsSchema schema = SettingsSchema::load(settings_document, &schema_error);
    if (schema.empty()) {
        if (args.settings) {
            std::cerr << "watashi_ui: cannot load " << settings_document << ": "
                      << schema_error << "\n";
            return 2;
        }
        std::cerr << "settings: " << settings_document << " unavailable ("
                  << schema_error << "); running with defaults\n";
    }
    SettingsStore store(
        std::move(schema),
        args.config_path.empty() ? "config/user.json" : args.config_path);
    std::vector<std::string> settings_warnings;
    std::string settings_error;
    if (!store.load(&settings_warnings, &settings_error)) {
        std::cerr << "settings: " << settings_error << "\n";
    }
    for (const auto& warning : settings_warnings) {
        std::cerr << "settings: " << warning << "\n";
    }
    const auto setting_string = [&store](const char* key, const char* fallback) {
        const nlohmann::json* value = store.find_value(key);
        return value != nullptr && value->is_string() ? value->get<std::string>()
                                                      : std::string(fallback);
    };
    const auto format_number = [](const nlohmann::json& value) {
        std::ostringstream stream;
        stream << value.get<double>();
        return stream.str();
    };

    Run run;
    run.capture_mode = args.capture;
    run.linger = args.linger;
    run.started = steady_seconds();
    run.foreground_before = GetForegroundWindow();

    bool explicit_path = false;
    const std::string spec_path = resolve_presentation_path(args, explicit_path);
    if (explicit_path) {
        std::error_code error;
        if (!std::filesystem::exists(spec_path, error)) {
            std::cerr << "watashi_ui: presentation file not found: " << spec_path
                      << "\n";
            return 2;
        }
    }
    run.presentation.open(spec_path);
    std::cerr << "presentation: "
              << (spec_path.empty() ? std::string("built-in bar")
                                    : spec_path)
              << " (name=" << run.presentation.spec().name
              << ", revision=" << run.presentation.revision() << ")\n";
    if (!run.presentation.error().empty()) {
        std::cerr << "presentation: " << run.presentation.error()
                  << "; keeping the built-in bar preset\n";
    }

    if (!run.window.create()) {
        std::cerr << "watashi_ui: no desktop session: CreateWindowExW failed ("
                  << run.window.last_error() << ")\n";
        return 3;
    }

    run.work_area = primary_work_area();
    run.dpi = window_dpi(run.window.hwnd());

    // Explicit flags win over config/user.json, which wins over the schema
    // defaults. Only the four engine-facing values are applied at startup;
    // every other field is restart-only and is honestly labelled that way.
    Args effective = args;
    if (effective.target_lang.empty()) {
        effective.target_lang = setting_string("translation.target", "zh-CN");
    }
    if (effective.scene.empty()) {
        effective.scene = setting_string("translation.scene", "");
    }
    if (effective.fps.empty()) {
        const nlohmann::json* value = store.find_value("capture.fps");
        if (value != nullptr && value->is_number()) {
            effective.fps = format_number(*value);
        }
    }
    if (effective.diff_threshold.empty()) {
        const nlohmann::json* value = store.find_value("capture.diff_threshold");
        if (value != nullptr && value->is_number()) {
            // The schema's 2.0 means 2 %; the engine's detector takes 0-1.
            const double fraction =
                std::max(0.0, std::min(1.0, value->get<double>() / 100.0));
            effective.diff_threshold = format_number(nlohmann::json(fraction));
        }
    }
    run.target_lang = effective.target_lang.empty() ? "zh-CN" : effective.target_lang;

    const std::vector<std::string> engine_args = engine_arguments(effective);
    ChildStream stream({cli, engine_args, {}, 6});
    run.stream = &stream;
    std::cerr << "engine: " << cli << "\n";

    const bool started = stream.start(
        [&run](const std::string& line) { run.model.ingest(line); },
        [&run](int exit_code, const std::string& stderr_tail) {
            run.model.end_stream(exit_code, stderr_tail, false);
        });
    if (!started) {
        run.launch_error = stream.error();
        run.model.set_launch_failure(run.launch_error);
        std::cerr << "watashi_ui: cannot start " << cli << ": "
                  << run.launch_error << "\n";
    }

    // Hotkeys are global: RegisterHotKey works while another window has focus,
    // and the callback only posts a command, it never activates this window.
    register_hotkeys(run);
    // The icon's callback window is the overlay itself; the menu owner is a
    // separate activatable window inside TrayIcon.
    run.tray_added = run.tray.add(run.window.hwnd());
    if (run.tray_added) {
        std::cerr << "tray: icon added\n";
    } else {
        std::cerr << "tray: unavailable in this session (Shell_NotifyIcon error "
                  << run.tray.last_error() << "); hotkeys still work\n";
    }
    std::cerr << "commands: Ctrl+Alt+P pause/resume, Ctrl+Alt+H hide/show, "
                 "Ctrl+Alt+T target language (" << run.target_lang << ")\n";

    // A separate top-level window. Closing it never closes the overlay; it is
    // destroyed only when the whole UI finishes. Its Save handler persists
    // config/user.json and sends the live commands the engine implements.
    std::unique_ptr<SettingsWindow> settings_window;
    if (args.settings) {
        SettingsWindowCallbacks callbacks;
        callbacks.send_command = [&run](const nlohmann::json& command) {
            return send_command(run, command);
        };
        callbacks.target_language_changed = [&run](const std::string& language) {
            run.target_lang = language;
        };
        callbacks.reload_corpus = [&run]() {
            return send_command(run, reload_corpus_command());
        };
        settings_window =
            std::make_unique<SettingsWindow>(store, std::move(callbacks));
        if (!settings_window->create()) {
            std::cerr << "settings: cannot create the settings window\n";
            settings_window.reset();
        } else {
            // Opening the settings window is an explicit focus change; the
            // overlay's click-through/focus evidence starts from here so it
            // still measures the overlay (which never activates) correctly.
            run.foreground_before = GetForegroundWindow();
        }
    }

    // Every normal exit -- tray Exit, WM_CLOSE, the stream ending on its own --
    // converges here. The report is written exactly once, after the child and
    // the tray are stopped, so ``tray.added`` and the stream counters are the
    // values the run actually measured.
    bool finished = false;
    auto finish = [&](int exit_code) -> int {
        if (finished) {
            return exit_code;
        }
        finished = true;
        if (settings_window) {
            settings_window->destroy();
        }
        unregister_hotkeys(run);
        run.tray.remove();
        stream.stop();

        const UiSnapshot snapshot = run.model.snapshot();
        if (!args.final_image.empty() && !run.image.empty()) {
            std::filesystem::path path(args.final_image);
            if (!path.parent_path().empty()) {
                std::filesystem::create_directories(path.parent_path());
            }
            if (write_bmp(path.string(), run.image)) {
                std::cerr << "final image: " << path.string() << "\n";
            } else {
                std::cerr << "watashi_ui: cannot write final image "
                          << path.string() << "\n";
            }
        }

        write_report(
            args, cli, engine_args, run, snapshot, exit_code, run.finish_reason);

        std::cerr << "ui: frames=" << snapshot.subtitles
                  << " malformed=" << snapshot.malformed
                  << " ignored=" << snapshot.ignored
                  << " gaps=" << snapshot.missing_records
                  << " out_of_order=" << snapshot.out_of_order
                  << " exit=" << snapshot.exit_code
                  << " | overlay=" << run.layout.width << 'x' << run.layout.height
                  << '+' << run.layout.x << '+' << run.layout.y
                  << " dpi=" << run.dpi
                  << " | click_through="
                  << ((run.window_under_center != nullptr &&
                       run.window_under_center != run.window.hwnd())
                          ? "pass"
                          : "unknown")
                  << " | memory_mib=" << run.memory.first << ".."
                  << run.memory.maximum
                  << " | reason=" << run.finish_reason << "\n";

        run.window.destroy();
        g_console_window.store(nullptr);
        g_console_finished.store(true);
        SetConsoleCtrlHandler(&console_control_handler, FALSE);
        return exit_code;
    };

    g_console_requested.store(false);
    g_console_finished.store(false);
    g_console_window.store(run.window.hwnd());
    SetConsoleCtrlHandler(&console_control_handler, TRUE);

    int exit_code = 0;
    try {
        // A small explicit loop instead of WM_TIMER: the tick has to run even
        // when no message arrives, and this keeps the window class free of UI
        // logic. WM_HOTKEY and the tray callback are handled here so the
        // overlay's window class stays a pure painter.
        while (true) {
            MSG message{};
            bool quit = false;
            while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
                if (message.message == WM_QUIT) {
                    quit = true;
                    break;
                }
                bool handled = false;
                if (message.message == WM_CLOSE &&
                    message.hwnd == run.window.hwnd()) {
                    // The console handler posts WM_CLOSE to the overlay; a
                    // direct close of the overlay takes the same path. Only the
                    // overlay is intercepted: closing the settings window must
                    // not close the application.
                    run.finish_reason =
                        g_console_requested.load() ? "console" : "wm-close";
                    run.quit_requested = true;
                    handled = true;
                } else if (message.message == WM_HOTKEY) {
                    handle_hotkey(run, static_cast<int>(message.wParam));
                    handled = true;
                } else if (message.hwnd == run.window.hwnd() &&
                           message.message == kTrayCallbackMessage) {
                    // NOTIFYICON_VERSION_4 carries the event in LOWORD(lParam);
                    // the older convention carries it as the whole lParam. LOWORD
                    // is correct for both.
                    const UINT event = LOWORD(message.lParam);
                    if (event == WM_RBUTTONUP || event == WM_CONTEXTMENU) {
                        const int chosen =
                            run.tray.show_menu(run.paused, !run.hidden_by_user);
                        if (chosen != 0) {
                            handle_tray_command(run, chosen);
                        }
                    }
                    handled = true;
                }
                if (!handled) {
                    TranslateMessage(&message);
                    DispatchMessageW(&message);
                }
            }
            if (quit || run.quit_requested) {
                break;
            }

            int new_dpi = 0;
            if (run.window.take_dpi_changed(new_dpi) && new_dpi > 0) {
                run.dpi = new_dpi;
                run.work_area = primary_work_area();
            }

            const double now = steady_seconds();
            const std::uint64_t spec_revision = run.presentation.revision();
            const std::string spec_error = run.presentation.error();
            run.presentation.poll(now);
            if (run.presentation.revision() != spec_revision) {
                std::cerr << "presentation: reloaded " << run.presentation.path()
                          << " (name=" << run.presentation.spec().name
                          << ", revision=" << run.presentation.revision() << ")\n";
            } else if (!run.presentation.error().empty() &&
                       run.presentation.error() != spec_error) {
                std::cerr << "presentation: " << run.presentation.error()
                          << "; keeping the previous design\n";
            }
            const UiSnapshot snapshot = run.model.snapshot();
            render_if_dirty(run, snapshot, now);
            sample_memory(run, now);
            schedule_quit_if_done(
                run, snapshot, now,
                settings_window != nullptr && settings_window->open());

            MsgWaitForMultipleObjects(0, nullptr, FALSE, kTimerIntervalMs, QS_ALLINPUT);
        }

        if (!run.presented) {
            // A stream that ended before the first tick still gets one honest
            // frame: "stream ended", with no subtitle invented.
            const UiSnapshot snapshot = run.model.snapshot();
            render_if_dirty(run, snapshot, steady_seconds());
        }

        // A requested stop (tray Exit, WM_CLOSE, Ctrl+C) happens while the
        // child is still running, so -1 is not a failure here: the process
        // ended because it was asked to. Only a failed launch or a nonzero
        // engine exit is an error.
        const UiSnapshot snapshot = run.model.snapshot();
        if (snapshot.launch_failed) {
            exit_code = 1;
        } else if (!snapshot.stream_ended) {
            exit_code = 0;
        } else {
            exit_code = snapshot.exit_code == 0 ? 0 : 1;
        }
    } catch (const std::exception& error) {
        // The report still gets written: an exception after the window exists
        // must not leave the run without a record.
        std::cerr << "watashi_ui: " << error.what() << "\n";
        run.finish_reason = "exception";
        exit_code = 1;
    }

    return finish(exit_code);
}

}  // namespace

int main(int argc, char** argv) {
    Args args;
    if (!parse_args(argc, argv, args)) {
        return 2;
    }
    // --version must not touch DPI, the engine or the log: it is a build stamp
    // report and has to stay usable on a machine without a compositor.
    if (args.version) {
        std::cout << watashi::obs::version_text() << std::flush;
        return 0;
    }

    // Observability is best-effort by construction: a log directory that cannot
    // be created disables the logger and changes nothing else. Only metadata is
    // recorded; subtitle text never reaches this file.
    watashi::obs::RingLog run_log(watashi::obs::LogOptions{watashi::obs::log_directory()});
    (void)run_log.open();
    const watashi::obs::VersionInfo build = watashi::obs::version();
    (void)run_log.write("info", "watashi_ui start version=" + build.version +
                                    " commit=" + build.commit +
                                    " build=" + build.build_type +
                                    " capture=" + args.capture);
    if (watashi::obs::dumps_enabled()) {
        (void)watashi::obs::install_crash_dump_handler(watashi::obs::dump_directory());
    }

    // Must run before any window exists: Windows locks DPI awareness in at
    // the first HWND or HDC.
    (void)watashi::enable_per_monitor_dpi();

    int exit_code = 1;
    try {
        // Preview is a pure render check: it needs no engine arguments and no
        // window, so it runs before live-capture validation.
        if (!args.preview_dir.empty()) {
            exit_code = run_preview(args);
        } else if (!validate_args(args)) {
            exit_code = 2;
        } else {
            exit_code = run_overlay(args);
        }
    } catch (const std::exception& error) {
        std::cerr << "watashi_ui: " << error.what() << "\n";
        (void)run_log.write("error", std::string("exception: ") + error.what());
        exit_code = 1;
    }
    (void)run_log.write("info", "watashi_ui exit code=" + std::to_string(exit_code));
    return exit_code;
}
