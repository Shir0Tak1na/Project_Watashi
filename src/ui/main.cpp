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
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "watashi/capture.hpp"
#include "watashi/ui/child_stream.hpp"
#include "watashi/ui/layered_window.hpp"
#include "watashi/ui/layout.hpp"
#include "watashi/ui/model.hpp"
#include "watashi/ui/renderer.hpp"

namespace {

using namespace watashi;
using namespace watashi::ui;

constexpr UINT kTimerIntervalMs = 50;
constexpr double kStaleAfterSeconds = 3.0;
constexpr double kMemorySampleSeconds = 5.0;

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
    double linger = 8.0;
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
           "  --preview DIR          render 96/120/144 dpi proofs and exit (no window)\n"
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

int run_preview(const Args& args) {
    const CaptureRect work_area = primary_work_area();
    if (work_area.width <= 0 || work_area.height <= 0) {
        std::cerr << "watashi_ui: no desktop session: no monitor reports a work area\n";
        return 3;
    }

    std::filesystem::create_directories(args.preview_dir);
    GdiText text;
    const TextMeasure measure = text.as_measure();
    std::vector<PreviewEntry> entries;

    for (int dpi : {96, 120, 144}) {
        LayoutRequest request;
        request.source = sample_source();
        request.target = sample_target();
        request.status = "preview · dpi=" + std::to_string(dpi) +
                         " · bottom-centre anchor";
        request.work_area = work_area;
        request.dpi = dpi;

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
        "live · " + capture_mode + " · " + std::to_string(snapshot.subtitles) +
        " frame(s)";
    if (snapshot.malformed > 0) {
        text += " · " + std::to_string(snapshot.malformed) + " bad line(s)";
    }
    if (snapshot.missing_records > 0) {
        text += " · " + std::to_string(snapshot.missing_records) + " missing";
    }
    if (snapshot.subtitles == 0) {
        text += " · waiting for subtitles";
    }
    const double quiet = now - snapshot.last_line_seconds;
    if (quiet > kStaleAfterSeconds) {
        text += " · no data for " + std::to_string(static_cast<int>(quiet)) + "s";
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

    std::uint64_t rendered_version = 0;
    bool has_version = false;
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

    bool click_checked = false;
    HWND foreground_before = nullptr;
    HWND foreground_after = nullptr;
    HWND window_under_center = nullptr;

    double last_memory_sample = 0.0;
    MemorySample memory;
};

void schedule_quit_if_done(Run& run, const UiSnapshot& snapshot, double now) {
    if (!snapshot.stream_ended) {
        return;
    }
    if (run.ended_at < 0.0) {
        run.ended_at = now;
    }
    // Always paint the final status at least once before closing.
    if (run.presented && now - run.ended_at >= run.linger) {
        run.quit_requested = true;
        PostQuitMessage(0);
    }
}

void render_if_dirty(Run& run, const UiSnapshot& snapshot, double now) {
    const std::string status = status_text(
        snapshot, run.launch_error, run.capture_mode, now);
    const bool dirty = !run.has_version ||
                       snapshot.version != run.rendered_version ||
                       status != run.rendered_status ||
                       run.dpi != run.rendered_dpi;
    if (!dirty) {
        return;
    }

    LayoutRequest request;
    // W3.0 publishes the frame's own source and target text. ``lines`` and
    // boxes are deliberately not used yet; this is a display, not a layout
    // engine.
    request.source = snapshot.frame.source;
    request.target = snapshot.frame.target;
    request.status = status;
    request.work_area = run.work_area;
    request.dpi = run.dpi;

    run.layout = compute_layout(request, run.text.as_measure());
    run.image = compose_overlay(run.layout, run.text);
    run.window.present(run.image, run.layout.x, run.layout.y);

    run.has_version = true;
    run.rendered_version = snapshot.version;
    run.rendered_status = status;
    run.rendered_dpi = run.dpi;
    run.presented = true;

    if (!run.click_checked && !run.layout.empty) {
        run.click_checked = true;
        POINT center{
            run.layout.x + run.layout.width / 2,
            run.layout.y + run.layout.height / 2,
        };
        run.window_under_center = WindowFromPoint(center);
        run.foreground_after = GetForegroundWindow();
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
    int exit_code) {
    if (args.report_path.empty()) {
        return;
    }
    nlohmann::json payload;
    payload["kind"] = "watashi-ui-run";
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
    payload["exit_code"] = exit_code;

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

int run_overlay(const Args& args) {
    const std::string cli =
        args.cli.empty() ? sibling_executable("watashi_cli.exe") : args.cli;
    if (!std::filesystem::exists(cli)) {
        std::cerr << "watashi_ui: engine binary not found: " << cli
                  << " (build it or pass --cli PATH)\n";
        return 2;
    }

    Run run;
    run.capture_mode = args.capture;
    run.linger = args.linger;
    run.started = steady_seconds();
    run.foreground_before = GetForegroundWindow();

    if (!run.window.create()) {
        std::cerr << "watashi_ui: no desktop session: CreateWindowExW failed ("
                  << run.window.last_error() << ")\n";
        return 3;
    }

    run.work_area = primary_work_area();
    run.dpi = window_dpi(run.window.hwnd());

    const std::vector<std::string> engine_args = engine_arguments(args);
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

    // A small explicit loop instead of WM_TIMER: the tick has to run even
    // when no message arrives, and this keeps the window class free of UI
    // logic. Messages (including WM_DPICHANGED) are dispatched first.
    while (true) {
        MSG message{};
        bool quit = false;
        while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
            if (message.message == WM_QUIT) {
                quit = true;
                break;
            }
            TranslateMessage(&message);
            DispatchMessageW(&message);
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
        const UiSnapshot snapshot = run.model.snapshot();
        render_if_dirty(run, snapshot, now);
        sample_memory(run, now);
        schedule_quit_if_done(run, snapshot, now);

        MsgWaitForMultipleObjects(0, nullptr, FALSE, kTimerIntervalMs, QS_ALLINPUT);
    }

    if (!run.presented) {
        // A stream that ended before the first tick still gets one honest
        // frame: "stream ended", with no subtitle invented.
        const UiSnapshot snapshot = run.model.snapshot();
        render_if_dirty(run, snapshot, steady_seconds());
    }

    stream.stop();

    const UiSnapshot snapshot = run.model.snapshot();
    const int exit_code = snapshot.launch_failed
                              ? 1
                              : (snapshot.exit_code == 0 ? 0 : 1);

    if (!args.final_image.empty() && !run.image.empty()) {
        std::filesystem::path path(args.final_image);
        if (!path.parent_path().empty()) {
            std::filesystem::create_directories(path.parent_path());
        }
        if (write_bmp(path.string(), run.image)) {
            std::cerr << "final image: " << path.string() << "\n";
        } else {
            std::cerr << "watashi_ui: cannot write final image " << path.string() << "\n";
        }
    }

    write_report(args, cli, engine_args, run, snapshot, exit_code);

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
              << " | memory_mib=" << run.memory.first << ".." << run.memory.maximum
              << "\n";

    run.window.destroy();
    return exit_code;
}

}  // namespace

int main(int argc, char** argv) {
    // Must run before any window exists: Windows locks DPI awareness in at
    // the first HWND or HDC.
    (void)watashi::enable_per_monitor_dpi();

    Args args;
    if (!parse_args(argc, argv, args)) {
        return 2;
    }

    try {
        // Preview is a pure render check: it needs no engine arguments and no
        // window, so it runs before live-capture validation.
        if (!args.preview_dir.empty()) {
            return run_preview(args);
        }
        if (!validate_args(args)) {
            return 2;
        }
        return run_overlay(args);
    } catch (const std::exception& error) {
        std::cerr << "watashi_ui: " << error.what() << "\n";
        return 1;
    }
}
