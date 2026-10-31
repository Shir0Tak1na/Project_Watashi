// Unit tests for the overlay that need no display.
//
// The display-dependent part lives in ui_window_test.cpp and exits 3 without a
// compositor. Everything here must pass on a headless machine: geometry, DPI,
// wrapping, envelope parsing, stream failures and window style bits.

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <catch2/catch_approx.hpp>
#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_floating_point.hpp>

#include <nlohmann/json.hpp>

#include "watashi/events.hpp"
#include "watashi/ui/child_stream.hpp"
#include "watashi/ui/layered_window.hpp"
#include "watashi/ui/layout.hpp"
#include "watashi/ui/model.hpp"
#include "watashi/ui/renderer.hpp"

namespace {

using namespace watashi;

ui::TextMeasure fake_measure(double width_per_px = 0.6) {
    return [width_per_px](const std::string& text, int px, bool bold) {
        ui::TextExtent extent;
        // Semibold is wider than regular; the layout must measure with the
        // same weight the renderer paints, or the right edge is clipped.
        const double weight = bold ? 1.15 : 1.0;
        extent.width = static_cast<int>(std::lround(
            static_cast<double>(text.size()) * px * width_per_px * weight));
        extent.height = px;
        return extent;
    };
}

ui::LayoutRequest request_with(
    const std::string& source,
    const std::string& target,
    const std::string& status,
    CaptureRect work_area,
    int dpi) {
    ui::LayoutRequest request;
    request.source = source;
    request.target = target;
    request.status = status;
    request.work_area = work_area;
    request.dpi = dpi;
    return request;
}

std::string envelope_line(
    const std::string& type,
    nlohmann::json data,
    std::int64_t seq) {
    const watashi::Envelope envelope =
        watashi::encode_event(type, std::move(data), seq, 1.0);
    return watashi::to_json_line(envelope);
}

std::string subtitle_line(
    const std::string& source,
    const std::string& target,
    std::int64_t seq) {
    return envelope_line(
        "subtitle",
        nlohmann::json{{"source", source}, {"target", target}, {"backend", "corpus"}},
        seq);
}

std::string fixture_executable() {
    wchar_t module[MAX_PATH]{};
    GetModuleFileNameW(nullptr, module, MAX_PATH);
    std::wstring path(module);
    const auto slash = path.find_last_of(L"\\/");
    path = path.substr(0, slash + 1) + L"watashi_ui_stream_fixture.exe";
    const int length = WideCharToMultiByte(
        CP_UTF8, 0, path.c_str(), static_cast<int>(path.size()), nullptr, 0, nullptr, nullptr);
    std::string utf8(static_cast<std::size_t>(length), '\0');
    WideCharToMultiByte(
        CP_UTF8, 0, path.c_str(), static_cast<int>(path.size()), utf8.data(), length,
        nullptr, nullptr);
    return utf8;
}

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

}  // namespace

TEST_CASE("dpi scale maps 96, 120 and 144 to 1.0, 1.25 and 1.5", "[ui][dpi]") {
    REQUIRE(ui::dpi_scale(96) == Catch::Approx(1.0));
    REQUIRE(ui::dpi_scale(120) == Catch::Approx(1.25));
    REQUIRE(ui::dpi_scale(144) == Catch::Approx(1.5));
    REQUIRE(ui::dpi_scale(72) == Catch::Approx(1.0));
    REQUIRE(ui::dpi_scale(0) == Catch::Approx(1.0));
}

TEST_CASE("logical pixels round once per edge", "[ui][dpi]") {
    REQUIRE(ui::logical_px(24, 96) == 24);
    REQUIRE(ui::logical_px(24, 120) == 30);
    REQUIRE(ui::logical_px(24, 144) == 36);
    // 3 * 144 / 96 = 4.5 -> 5 (half up), not 4.
    REQUIRE(ui::logical_px(3, 144) == 5);
    REQUIRE(ui::logical_px(15, 120) == 19);  // 18.75 -> 19
}

TEST_CASE("bottom-centre anchor at 100 percent", "[ui][anchor]") {
    const CaptureRect work{0, 0, 1920, 1080};
    const CaptureRect placed = ui::anchor_bottom_center(work, 600, 120, 80, 24);
    REQUIRE(placed.x == 660);
    REQUIRE(placed.y == 880);
    REQUIRE(placed.width == 600);
    REQUIRE(placed.height == 120);
}

TEST_CASE("bottom-centre anchor at 125 percent", "[ui][anchor]") {
    // The overlay grew by 1.25; the centre and the scaled margin must follow.
    const CaptureRect work{0, 0, 1920, 1080};
    const CaptureRect placed = ui::anchor_bottom_center(work, 750, 150, 100, 30);
    REQUIRE(placed.x == 585);
    REQUIRE(placed.y == 830);
}

TEST_CASE("bottom-centre anchor at 150 percent on a 2560x1600 work area", "[ui][anchor]") {
    const CaptureRect work{0, 0, 2560, 1600};
    const CaptureRect placed = ui::anchor_bottom_center(work, 900, 180, 165, 36);
    REQUIRE(placed.x == 830);
    REQUIRE(placed.y == 1255);
}

TEST_CASE("anchor keeps the centre on a monitor with a negative origin", "[ui][anchor]") {
    // A monitor placed left of the primary starts at x=-1920.
    const CaptureRect work{-1920, 0, 1920, 1080};
    const CaptureRect placed = ui::anchor_bottom_center(work, 600, 120, 80, 24);
    REQUIRE(placed.x == -1260);
    REQUIRE(placed.y == 880);
}

TEST_CASE("anchor clamps a too-wide overlay to the work area", "[ui][anchor]") {
    const CaptureRect work{0, 0, 800, 600};
    const CaptureRect placed = ui::anchor_bottom_center(work, 900, 120, 40, 24);
    REQUIRE(placed.x == 0);
    REQUIRE(placed.y == 440);
}

TEST_CASE("anchor never places the overlay above the work area", "[ui][anchor]") {
    const CaptureRect work{0, 0, 800, 200};
    const CaptureRect placed = ui::anchor_bottom_center(work, 600, 300, 40, 24);
    REQUIRE(placed.y == 0);
}

TEST_CASE("wrap_text breaks greedily at spaces", "[ui][layout]") {
    const ui::TextMeasure measure = fake_measure();
    // 10 px font, 6 px per character: each 10-char word is exactly 60 px.
    const auto lines = ui::wrap_text(
        "aaaaaaaaaa bbbbbbbbbb cccccccccc", 100, 10, false, measure);
    REQUIRE(lines.size() == 3);
    REQUIRE(lines[0] == "aaaaaaaaaa");
    REQUIRE(lines[1] == "bbbbbbbbbb");
    REQUIRE(lines[2] == "cccccccccc");
}

TEST_CASE("wrap_text keeps explicit newlines", "[ui][layout]") {
    const ui::TextMeasure measure = fake_measure();
    const auto lines = ui::wrap_text("first line\nsecond line", 1000, 10, false, measure);
    REQUIRE(lines.size() == 2);
    REQUIRE(lines[0] == "first line");
    REQUIRE(lines[1] == "second line");
}

TEST_CASE("wrap_text hard-splits a token wider than the line", "[ui][layout]") {
    const ui::TextMeasure measure = fake_measure();
    // 30 chars at 6 px each: 180 px against a 60 px line.
    const auto lines = ui::wrap_text(std::string(30, 'x'), 60, 10, false, measure);
    REQUIRE(lines.size() == 3);
    for (const auto& line : lines) {
        REQUIRE(line.size() == 10);
    }
}

TEST_CASE("wrap_text measures the target with its painted semibold weight", "[ui][layout][clip]") {
    const ui::TextMeasure measure = fake_measure();
    // 40 chars are 576 px in regular but 662 px in the 1.15x bold metric.
    const std::string word(40, 'x');
    REQUIRE(ui::wrap_text(word, 600, 24, false, measure).size() == 1);
    const auto bold = ui::wrap_text(word, 600, 24, true, measure);
    REQUIRE(bold.size() == 2);
    for (const auto& line : bold) {
        REQUIRE(measure(line, 24, true).width <= 600);
    }
}

TEST_CASE("target runs fit inside the plate at the weight they are painted", "[ui][layout][clip]") {
    const ui::TextMeasure measure = fake_measure();
    const CaptureRect work{0, 0, 1920, 1080};
    const std::string target =
        "this sect's sword intent is only a legend and good game, well played, beginner";
    const ui::Layout layout =
        ui::compute_layout(request_with("", target, "", work, 96), measure);
    const int padding = ui::logical_px(ui::Style{}.padding_x, 96);

    REQUIRE_FALSE(layout.empty);
    REQUIRE_FALSE(layout.runs.empty());
    for (const auto& run : layout.runs) {
        REQUIRE(run.kind == ui::RunKind::kTarget);
        // The width carried into the compositing box must be the bold width;
        // a regular measurement here was the right-edge clipping bug.
        REQUIRE(run.width == measure(run.text, run.px, true).width);
        REQUIRE(run.x + run.width <= layout.width - padding);
    }
}

TEST_CASE("renderer measures semibold text at the weight it paints", "[ui][render][clip]") {
    ui::GdiText text;
    const auto measure = text.as_measure();
    const std::string line = "this sect's sword intent is only a legend";
    const ui::TextExtent regular = measure(line, 36, false);
    const ui::TextExtent bold = measure(line, 36, true);
    REQUIRE(bold.width >= regular.width);
}

TEST_CASE("a long target sentence fits the plate with real 150% metrics", "[ui][render][clip]") {
    ui::GdiText text;
    const auto measure = text.as_measure();
    ui::LayoutRequest request;
    request.target = "this sect's sword intent is only a legend";
    request.work_area = CaptureRect{0, 0, 2560, 1600};
    request.dpi = 144;
    const ui::Layout layout = ui::compute_layout(request, measure);
    const int padding = ui::logical_px(ui::Style{}.padding_x, 144);

    REQUIRE_FALSE(layout.empty);
    for (const auto& run : layout.runs) {
        REQUIRE(run.kind == ui::RunKind::kTarget);
        REQUIRE(run.width == measure(run.text, run.px, true).width);
        REQUIRE(run.x + run.width <= layout.width - padding);
    }
}

TEST_CASE("compute_layout emits source then target runs, wrapping both", "[ui][layout]") {
    const ui::TextMeasure measure = fake_measure();
    const CaptureRect work{0, 0, 1920, 1080};
    const ui::Layout layout = ui::compute_layout(
        request_with(
            "the sword intent of this sect is a myth\nhe broke through to the void realm",
            "本门剑意只是传说",
            "",
            work,
            96),
        measure);

    REQUIRE_FALSE(layout.empty);
    REQUIRE(layout.source_lines == 2);
    REQUIRE(layout.target_lines == 1);
    REQUIRE(layout.runs.size() == 3);
    REQUIRE(layout.runs[0].kind == ui::RunKind::kSource);
    REQUIRE(layout.runs[1].kind == ui::RunKind::kSource);
    REQUIRE(layout.runs[2].kind == ui::RunKind::kTarget);
    REQUIRE(layout.runs[2].text == "本门剑意只是传说");

    // The anchor is applied to the final size.
    REQUIRE(layout.x == work.x + (work.width - layout.width) / 2);
    REQUIRE(layout.y + layout.height ==
                work.bottom() - ui::logical_px(ui::Style{}.margin_bottom, 96));
}

TEST_CASE("compute_layout treats empty content as nothing to paint", "[ui][layout]") {
    const ui::Layout layout = ui::compute_layout(
        request_with("", "", "", CaptureRect{0, 0, 1920, 1080}, 96),
        fake_measure());
    REQUIRE(layout.empty);
    REQUIRE(layout.width == 0);
    REQUIRE(layout.height == 0);
    REQUIRE(layout.runs.empty());
}

TEST_CASE("compute_layout still paints the status with no subtitle", "[ui][layout]") {
    const ui::Layout layout = ui::compute_layout(
        request_with("", "", "stream ended (exit 0)", CaptureRect{0, 0, 1920, 1080}, 96),
        fake_measure());
    REQUIRE_FALSE(layout.empty);
    REQUIRE(layout.runs.size() == 1);
    REQUIRE(layout.runs[0].kind == ui::RunKind::kStatus);
    REQUIRE(layout.runs[0].text == "stream ended (exit 0)");
}

TEST_CASE("layout scales every metric by the dpi", "[ui][layout]") {
    const ui::TextMeasure measure = fake_measure();
    const CaptureRect work{0, 0, 2560, 1600};
    const ui::Layout hundred = ui::compute_layout(
        request_with("hello", "hello", "live", work, 96), measure);
    const ui::Layout hundred_fifty = ui::compute_layout(
        request_with("hello", "hello", "live", work, 144), measure);

    REQUIRE(hundred_fifty.width >= hundred.width * 1.4);
    REQUIRE(hundred_fifty.height >= hundred.height * 1.4);
    REQUIRE(hundred_fifty.runs.front().px == ui::logical_px(15, 144));
}

TEST_CASE("window styles carry all five required extended bits", "[ui][window]") {
    const ui::WindowStyles styles = ui::overlay_styles();
    const DWORD required = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST |
                           WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW;
    REQUIRE((styles.extended_style & required) == required);
    REQUIRE(styles.style == WS_POPUP);
    REQUIRE((styles.style & WS_CAPTION) == 0);
    REQUIRE((styles.style & WS_THICKFRAME) == 0);
}

TEST_CASE("parse_stream_line reads a subtitle envelope", "[ui][model]") {
    const std::string line = subtitle_line("the myth", "只是传说", 3);
    const ui::StreamLine parsed = ui::parse_stream_line(line);
    REQUIRE(parsed.kind == ui::StreamLineKind::kSubtitle);
    REQUIRE(parsed.seq == 3);
    REQUIRE(parsed.frame.valid);
    REQUIRE(parsed.frame.source == "the myth");
    REQUIRE(parsed.frame.target == "只是传说");
    REQUIRE(parsed.frame.backend == "corpus");
}

TEST_CASE("parse_stream_line rejects bad JSON and contract violations", "[ui][model]") {
    const ui::StreamLine bad_json = ui::parse_stream_line("{not json");
    REQUIRE(bad_json.kind == ui::StreamLineKind::kMalformed);
    REQUIRE_FALSE(bad_json.error.empty());

    const ui::StreamLine unknown = ui::parse_stream_line(
        R"({"v":1,"type":"watashi","ts":1,"seq":1,"data":{}})");
    REQUIRE(unknown.kind == ui::StreamLineKind::kMalformed);

    const ui::StreamLine no_target = ui::parse_stream_line(
        R"({"v":1,"type":"subtitle","ts":1,"seq":1,"data":{"source":"x"}})");
    REQUIRE(no_target.kind == ui::StreamLineKind::kMalformed);
}

TEST_CASE("parse_stream_line ignores valid non-subtitle events", "[ui][model]") {
    const std::string line = envelope_line(
        "stats", nlohmann::json{{"frames", 4}}, 2);
    const ui::StreamLine parsed = ui::parse_stream_line(line);
    REQUIRE(parsed.kind == ui::StreamLineKind::kIgnored);
    REQUIRE(parsed.seq == 2);
    REQUIRE(parsed.type == "stats");
}

TEST_CASE("model counts malformed lines but keeps the last frame", "[ui][model]") {
    ui::UiModel model;
    model.ingest(subtitle_line("sword intent", "剑意", 1));
    model.ingest("garbage without a brace");

    const ui::UiSnapshot snapshot = model.snapshot();
    REQUIRE(snapshot.malformed == 1);
    REQUIRE(snapshot.has_frame);
    REQUIRE(snapshot.frame.target == "剑意");
    REQUIRE_FALSE(snapshot.last_error.empty());
}

TEST_CASE("model counts missing and out-of-order seq values", "[ui][model]") {
    ui::UiModel model;
    model.ingest(subtitle_line("a", "A", 1));
    model.ingest(subtitle_line("b", "B", 5));
    model.ingest(subtitle_line("c", "C", 3));
    const ui::UiSnapshot snapshot = model.snapshot();
    REQUIRE(snapshot.subtitles == 3);
    REQUIRE(snapshot.missing_records == 3);
    REQUIRE(snapshot.out_of_order == 1);
    REQUIRE(snapshot.last_seq == 5);
}

TEST_CASE("model reports the exit code and stderr tail", "[ui][model]") {
    ui::UiModel model;
    model.ingest(subtitle_line("a", "A", 1));
    model.end_stream(7, "nmt: model missing", false);
    const ui::UiSnapshot snapshot = model.snapshot();
    REQUIRE(snapshot.stream_ended);
    REQUIRE(snapshot.exit_code == 7);
    REQUIRE(snapshot.stderr_tail == "nmt: model missing");
    REQUIRE(snapshot.has_frame);
    REQUIRE(snapshot.frame.target == "A");
}

TEST_CASE("model records a launch failure without inventing a frame", "[ui][model]") {
    ui::UiModel model;
    model.set_launch_failure("CreateProcessW failed: 2");
    const ui::UiSnapshot snapshot = model.snapshot();
    REQUIRE(snapshot.launch_failed);
    REQUIRE(snapshot.stream_ended);
    REQUIRE_FALSE(snapshot.has_frame);
}

TEST_CASE("child stream delivers lines and the exit code", "[ui][stream]") {
    std::mutex mutex;
    std::vector<std::string> lines;
    bool exited = false;
    int code = -1;
    std::string tail;

    ui::ChildStream stream(
        ui::ChildStream::Options{fixture_executable(), {"--count", "3", "--exit-code", "7"}, "", 6});
    const bool started = stream.start(
        [&](const std::string& line) {
            std::lock_guard<std::mutex> lock(mutex);
            lines.push_back(line);
        },
        [&](int exit_code, const std::string& stderr_tail) {
            std::lock_guard<std::mutex> lock(mutex);
            code = exit_code;
            tail = stderr_tail;
            exited = true;
        });
    REQUIRE(started);
    REQUIRE(wait_until([&] {
        std::lock_guard<std::mutex> lock(mutex);
        return exited;
    }, 5000));
    stream.stop();

    std::lock_guard<std::mutex> lock(mutex);
    REQUIRE(code == 7);
    REQUIRE(lines.size() == 3);
    REQUIRE(lines[0] == "line 1");
    REQUIRE(lines[2] == "line 3");
    REQUIRE(exited);
}

TEST_CASE("child stream emits a line that has no trailing newline", "[ui][stream]") {
    std::mutex mutex;
    std::vector<std::string> lines;
    bool exited = false;

    ui::ChildStream stream(ui::ChildStream::Options{
        fixture_executable(), {"--count", "2", "--no-final-newline"}, "", 6});
    REQUIRE(stream.start(
        [&](const std::string& line) {
            std::lock_guard<std::mutex> lock(mutex);
            lines.push_back(line);
        },
        [&](int, const std::string&) {
            std::lock_guard<std::mutex> lock(mutex);
            exited = true;
        }));
    REQUIRE(wait_until([&] {
        std::lock_guard<std::mutex> lock(mutex);
        return exited;
    }, 5000));
    stream.stop();

    std::lock_guard<std::mutex> lock(mutex);
    REQUIRE(lines.size() == 2);
    REQUIRE(lines.back() == "line 2");
}

TEST_CASE("child stream keeps only the last stderr lines", "[ui][stream]") {
    std::mutex mutex;
    std::string tail;
    bool exited = false;

    ui::ChildStream stream(ui::ChildStream::Options{
        fixture_executable(),
        {"--count", "0", "--stderr-lines", "4", "--stderr", "warn"},
        "",
        2});
    REQUIRE(stream.start(
        [](const std::string&) {},
        [&](int, const std::string& stderr_tail) {
            std::lock_guard<std::mutex> lock(mutex);
            tail = stderr_tail;
            exited = true;
        }));
    REQUIRE(wait_until([&] {
        std::lock_guard<std::mutex> lock(mutex);
        return exited;
    }, 5000));
    stream.stop();

    std::lock_guard<std::mutex> lock(mutex);
    REQUIRE(tail == "warn 3 | warn 4");
}

TEST_CASE("child stream reports a failed launch instead of pretending", "[ui][stream]") {
    ui::ChildStream stream(ui::ChildStream::Options{
        "Z:\\does-not-exist\\watashi_cli.exe", {}, "", 6});
    REQUIRE_FALSE(stream.start(
        [](const std::string&) {},
        [](int, const std::string&) {}));
    REQUIRE(stream.error().find("CreateProcessW") != std::string::npos);
    REQUIRE_FALSE(stream.running());
}

TEST_CASE("child stream stop terminates a long-running child", "[ui][stream]") {
    std::mutex mutex;
    int line_count = 0;
    bool exited = false;

    ui::ChildStream stream(ui::ChildStream::Options{
        fixture_executable(), {"--count", "1000", "--sleep-ms", "3000"}, "", 6});
    REQUIRE(stream.start(
        [&](const std::string&) {
            std::lock_guard<std::mutex> lock(mutex);
            ++line_count;
        },
        [&](int, const std::string&) {
            std::lock_guard<std::mutex> lock(mutex);
            exited = true;
        }));
    REQUIRE(wait_until([&] {
        std::lock_guard<std::mutex> lock(mutex);
        return line_count > 10;
    }, 3000));
    stream.stop();

    std::lock_guard<std::mutex> lock(mutex);
    // stop() is a deliberate shutdown: no exit callback is delivered.
    REQUIRE_FALSE(exited);
    REQUIRE_FALSE(stream.running());
}

TEST_CASE("renderer produces a premultiplied image for a layout", "[ui][render]") {
    ui::GdiText text;
    const ui::Layout layout = ui::compute_layout(
        request_with("the myth", "只是传说", "live", CaptureRect{0, 0, 1920, 1080}, 96),
        text.as_measure());
    REQUIRE_FALSE(layout.empty);

    const ui::OverlayImage image = ui::compose_overlay(layout, text);
    REQUIRE(image.width == layout.width);
    REQUIRE(image.height == layout.height);
    REQUIRE(image.bgra.size() ==
            static_cast<std::size_t>(image.width) * image.height * 4);

    // The plate must be translucent and premultiplied: alpha < 255 somewhere,
    // and no colour channel exceeds its alpha. Text pixels also satisfy the
    // latter by construction.
    bool saw_alpha = false;
    bool premultiplied = true;
    for (std::size_t index = 0; index < image.bgra.size(); index += 4) {
        const std::uint8_t alpha = image.bgra[index + 3];
        if (alpha > 0 && alpha < 255) {
            saw_alpha = true;
        }
        if (image.bgra[index + 0] > alpha || image.bgra[index + 1] > alpha ||
            image.bgra[index + 2] > alpha) {
            premultiplied = false;
            break;
        }
    }
    REQUIRE(premultiplied);
    REQUIRE(saw_alpha);
}
