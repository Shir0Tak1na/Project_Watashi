// Tests for the declarative presentation spec: parsing, validation, defaults,
// hot reload, anchoring and the style the layout hands to the renderer.
//
// None of these need a display. They cover the contract config/presentation.json
// describes so a broken hand-edited file can never take the overlay down.

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
#include <filesystem>
#include <fstream>
#include <random>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

#include <catch2/catch_approx.hpp>
#include <catch2/catch_test_macros.hpp>

#include "watashi/capture.hpp"
#include "watashi/ui/layout.hpp"
#include "watashi/ui/presentation.hpp"
#include "watashi/ui/renderer.hpp"

namespace {

using namespace watashi;

ui::TextMeasure fake_measure(double width_per_px = 0.6) {
    return [width_per_px](const std::string& text, const ui::MeasureStyle& style) {
        ui::TextExtent extent;
        const double weight = style.bold ? 1.15 : 1.0;
        extent.width = static_cast<int>(std::lround(
            static_cast<double>(text.size()) * style.px * width_per_px * weight));
        extent.height = style.px;
        return extent;
    };
}

ui::MeasureStyle style_of(int px, bool bold = false) {
    return ui::MeasureStyle{px, bold, false, ""};
}

/// One throwaway spec file, removed when the test ends.
class TempSpec {
public:
    TempSpec() {
        std::random_device device;
        std::mt19937 generator(device());
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        path_ = std::filesystem::temp_directory_path() /
                ("watashi_spec_" + std::to_string(stamp) + "_" +
                 std::to_string(generator()) + ".json");
    }

    ~TempSpec() {
        std::error_code error;
        std::filesystem::remove(path_, error);
    }

    TempSpec(const TempSpec&) = delete;
    TempSpec& operator=(const TempSpec&) = delete;

    std::string write(std::string_view contents) const {
        std::ofstream file(path_, std::ios::binary | std::ios::trunc);
        file.write(contents.data(), static_cast<std::streamsize>(contents.size()));
        file.close();
        return path_.string();
    }

    void remove() const {
        std::error_code error;
        std::filesystem::remove(path_, error);
    }

    [[nodiscard]] std::string path() const { return path_.string(); }

private:
    std::filesystem::path path_;
};

std::string full_spec(std::string target_size = "20") {
    return std::string(R"({
  "version": 1,
  "name": "custom",
  "layout": {
    "mode": "bar",
    "anchor": "top-left",
    "offset": [24, 18],
    "max_width_ratio": 0.5,
    "line_gap": 4,
    "align": "right"
  },
  "elements": [
    {"role": "source", "order": 0, "font": {"size": 14, "bold": false},
     "color": "#112233", "opacity": 0.8},
    {"role": "target", "order": 1, "font": {"size": )") +
           target_size +
           R"(, "bold": true, "italic": true},
     "color": "#abcdef", "opacity": 0.9, "max_lines": 2}
  ],
  "background": {"kind": "none", "color": "#010203", "opacity": 0.4,
                 "radius": 3, "padding": [6, 5]},
  "confidence": {"dim_below": 0.6, "dim_opacity": 0.35},
  "per_line": false
})";
}

}  // namespace

TEST_CASE("bar preset matches the shipped defaults", "[presentation]") {
    const ui::PresentationSpec spec = ui::PresentationSpec::bar();
    REQUIRE(spec.version == 1);
    REQUIRE(spec.name == "bar");
    REQUIRE(spec.layout.mode == ui::LayoutMode::kBar);
    REQUIRE(spec.layout.anchor == ui::Anchor::kBottomCenter);
    REQUIRE(spec.layout.offset_x == 0);
    REQUIRE(spec.layout.offset_y == -90);
    REQUIRE(spec.layout.max_width_ratio == Catch::Approx(0.9));
    REQUIRE(spec.layout.line_gap == 6);
    REQUIRE(spec.layout.align == ui::Align::kCenter);
    REQUIRE(spec.background.kind == ui::BackgroundKind::kPlate);
    REQUIRE(spec.background.opacity == Catch::Approx(0.72));
    REQUIRE(spec.background.radius == 8);
    REQUIRE(spec.background.padding_x == 12);
    REQUIRE(spec.background.padding_y == 8);
    REQUIRE(spec.confidence.dim_below == Catch::Approx(0.5));
    REQUIRE(spec.confidence.dim_opacity == Catch::Approx(0.6));

    const auto* previous = spec.find("previous");
    const auto* source = spec.find("source");
    const auto* target = spec.find("target");
    REQUIRE(previous != nullptr);
    REQUIRE(source != nullptr);
    REQUIRE(target != nullptr);
    REQUIRE(previous->font.size == 13);
    REQUIRE(source->font.size == 16);
    REQUIRE(target->font.size == 24);
    REQUIRE(target->font.bold);
    REQUIRE(target->color.r == 255);
    REQUIRE(target->color.g == 255);
    REQUIRE(target->color.b == 255);
}

TEST_CASE("a full spec parses into the expected values", "[presentation]") {
    const ui::SpecLoad loaded =
        ui::parse_presentation_text(full_spec("20"), "full.json");
    REQUIRE(loaded.ok);
    const auto& spec = loaded.spec;
    REQUIRE(spec.name == "custom");
    REQUIRE(spec.layout.anchor == ui::Anchor::kTopLeft);
    REQUIRE(spec.layout.offset_x == 24);
    REQUIRE(spec.layout.offset_y == 18);
    REQUIRE(spec.layout.max_width_ratio == Catch::Approx(0.5));
    REQUIRE(spec.layout.line_gap == 4);
    REQUIRE(spec.layout.align == ui::Align::kRight);
    REQUIRE(spec.elements.size() == 2);
    const auto* target = spec.find("target");
    REQUIRE(target != nullptr);
    REQUIRE(target->font.size == 20);
    REQUIRE(target->font.bold);
    REQUIRE(target->font.italic);
    REQUIRE(target->color.r == 0xab);
    REQUIRE(target->color.g == 0xcd);
    REQUIRE(target->color.b == 0xef);
    REQUIRE(target->opacity == Catch::Approx(0.9));
    REQUIRE(target->max_lines == 2);
    REQUIRE(spec.background.kind == ui::BackgroundKind::kNone);
    REQUIRE(spec.background.padding_x == 6);
    REQUIRE(spec.confidence.dim_below == Catch::Approx(0.6));
}

TEST_CASE("unknown fields are ignored", "[presentation]") {
    const std::string text = R"({
      "future": {"whatever": 1},
      "layout": {"anchor": "bottom-center", "sparkle": true},
      "elements": [{"role": "target", "font": {"size": 24}, "glow": "yes"}]
    })";
    const ui::SpecLoad loaded = ui::parse_presentation_text(text, "extra.json");
    REQUIRE(loaded.ok);
    REQUIRE(loaded.spec.find("target") != nullptr);
}

TEST_CASE("empty objects fall back to the bar values", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text("{}", "empty.json");
    REQUIRE(loaded.ok);
    REQUIRE(loaded.spec.name == "bar");
    REQUIRE(loaded.spec.layout.anchor == ui::Anchor::kBottomCenter);
    REQUIRE(loaded.spec.elements.size() == 3);
    REQUIRE(loaded.spec.background.kind == ui::BackgroundKind::kPlate);
}

TEST_CASE("invalid JSON reports a line and column", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text(
        "{\n  \"name\": \"x\",\n}", "broken.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.line >= 2);
    REQUIRE(loaded.error.column >= 1);
    REQUIRE_FALSE(loaded.error.message.empty());
}

TEST_CASE("a non-object top level is rejected", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text("[1, 2]", "array.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.message.find("JSON object") != std::string::npos);
}

TEST_CASE("an unknown layout mode is rejected with its location", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text(
        "{\n  \"layout\": {\n    \"mode\": \"spiral\"\n  }\n}", "mode.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.message.find("layout mode") != std::string::npos);
    REQUIRE(loaded.error.line == 3);
    REQUIRE(loaded.error.column > 1);
}

TEST_CASE("an unknown anchor is rejected", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text(
        "{\"layout\": {\"anchor\": \"bottom-middle\"}}", "anchor.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.message.find("anchor") != std::string::npos);
}

TEST_CASE("an unknown alignment is rejected", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text(
        "{\"layout\": {\"align\": \"justify\"}}", "align.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.message.find("alignment") != std::string::npos);
}

TEST_CASE("offset must be a pair of integers", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text(
        "{\"layout\": {\"offset\": [12]}}", "offset.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.message.find("offset") != std::string::npos);
}

TEST_CASE("max_width_ratio outside 0..1 is rejected", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text(
        "{\"layout\": {\"max_width_ratio\": 1.5}}", "ratio.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.message.find("max_width_ratio") != std::string::npos);
}

TEST_CASE("a negative line_gap is rejected", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text(
        "{\"layout\": {\"line_gap\": -4}}", "gap.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.message.find("line_gap") != std::string::npos);
}

TEST_CASE("an unknown element role is rejected", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text(
        R"({"elements": [{"role": "caption"}]})", "role.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.message.find("role") != std::string::npos);
}

TEST_CASE("a font size below 4 is rejected", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text(
        R"({"elements": [{"role": "target", "font": {"size": 2}}]})", "size.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.message.find("size") != std::string::npos);
}

TEST_CASE("bold must be a boolean", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text(
        R"({"elements": [{"role": "target", "font": {"bold": "yes"}}]})",
        "bold.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.message.find("bold") != std::string::npos);
}

TEST_CASE("colours must be #rrggbb", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text(
        R"({"elements": [{"role": "target", "color": "#12345"}]})", "color.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.message.find("colour") != std::string::npos);
}

TEST_CASE("opacity outside 0..1 is rejected", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text(
        R"({"elements": [{"role": "target", "opacity": 1.5}]})", "opacity.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.message.find("opacity") != std::string::npos);
}

TEST_CASE("outline width must not be negative", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text(
        R"({"elements": [{"role": "target", "outline": {"width": -2}}]})",
        "outline.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.message.find("width") != std::string::npos);
}

TEST_CASE("background kind accepts plate, none and transparent", "[presentation]") {
    for (const char* kind : {"plate", "none", "transparent"}) {
        const std::string text =
            std::string("{\"background\": {\"kind\": \"") + kind + "\"}}";
        const ui::SpecLoad loaded = ui::parse_presentation_text(text, "kind.json");
        REQUIRE(loaded.ok);
        const ui::BackgroundKind expected =
            std::string(kind) == "plate" ? ui::BackgroundKind::kPlate
                                         : ui::BackgroundKind::kNone;
        REQUIRE(loaded.spec.background.kind == expected);
    }
    const ui::SpecLoad bad = ui::parse_presentation_text(
        "{\"background\": {\"kind\": \"blur\"}}", "kind.json");
    REQUIRE_FALSE(bad.ok);
    REQUIRE(bad.error.message.find("kind") != std::string::npos);
}

TEST_CASE("padding must be two non-negative integers", "[presentation]") {
    const ui::SpecLoad short_pair = ui::parse_presentation_text(
        "{\"background\": {\"padding\": [12]}}", "padding.json");
    REQUIRE_FALSE(short_pair.ok);
    REQUIRE(short_pair.error.message.find("padding") != std::string::npos);

    const ui::SpecLoad negative = ui::parse_presentation_text(
        "{\"background\": {\"padding\": [-1, 8]}}", "padding.json");
    REQUIRE_FALSE(negative.ok);
}

TEST_CASE("confidence values outside 0..1 are rejected", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text(
        "{\"confidence\": {\"dim_below\": 2.0}}", "confidence.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.message.find("dim_below") != std::string::npos);
}

TEST_CASE("max_lines must not be negative", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text(
        R"({"elements": [{"role": "target", "max_lines": -1}]})", "maxlines.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.message.find("max_lines") != std::string::npos);
}

TEST_CASE("elements must be an array", "[presentation]") {
    const ui::SpecLoad loaded = ui::parse_presentation_text(
        "{\"elements\": {}}", "elements.json");
    REQUIRE_FALSE(loaded.ok);
    REQUIRE(loaded.error.message.find("elements") != std::string::npos);
}

TEST_CASE("the watcher loads a valid file", "[presentation][reload]") {
    TempSpec file;
    file.write(full_spec("20"));
    ui::PresentationWatcher watcher(file.path());
    REQUIRE(watcher.has_file());
    REQUIRE(watcher.revision() == 1);
    REQUIRE(watcher.error().empty());
    const auto* target = watcher.spec().find("target");
    REQUIRE(target != nullptr);
    REQUIRE(target->font.size == 20);
}

TEST_CASE("the watcher keeps the bar preset when the first read is invalid",
          "[presentation][reload]") {
    TempSpec file;
    file.write("{\n  \"name\": ");
    ui::PresentationWatcher watcher(file.path());
    REQUIRE(watcher.revision() == 0);
    REQUIRE_FALSE(watcher.error().empty());
    REQUIRE(watcher.spec().name == "bar");
}

TEST_CASE("a valid save reloads within one poll", "[presentation][reload]") {
    TempSpec file;
    file.write(full_spec("20"));
    ui::PresentationWatcher watcher(file.path());
    REQUIRE(watcher.revision() == 1);

    file.write(full_spec("128"));
    REQUIRE(watcher.poll(1.0));
    REQUIRE(watcher.revision() == 2);
    REQUIRE(watcher.spec().find("target")->font.size == 128);
    REQUIRE(watcher.error().empty());

    // Same file, same size and mtime: nothing to do.
    REQUIRE_FALSE(watcher.poll(2.0));
    REQUIRE(watcher.revision() == 2);
}

TEST_CASE("an invalid save keeps the previous spec and reports once",
          "[presentation][reload]") {
    TempSpec file;
    file.write(full_spec("32"));
    ui::PresentationWatcher watcher(file.path());
    REQUIRE(watcher.revision() == 1);

    file.write(full_spec("40").substr(0, 40));  // half a JSON document
    REQUIRE_FALSE(watcher.poll(1.0));
    REQUIRE(watcher.revision() == 1);
    REQUIRE(watcher.spec().find("target")->font.size == 32);
    REQUIRE_FALSE(watcher.error().empty());
    REQUIRE(watcher.error().find("line") != std::string::npos);

    file.write(full_spec("40"));
    REQUIRE(watcher.poll(2.0));
    REQUIRE(watcher.revision() == 2);
    REQUIRE(watcher.spec().find("target")->font.size == 40);
    REQUIRE(watcher.error().empty());
}

TEST_CASE("a missing file keeps the last spec and reports it", "[presentation][reload]") {
    const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
    const std::string missing =
        (std::filesystem::temp_directory_path() /
         ("watashi_spec_missing_" + std::to_string(stamp) + ".json"))
            .string();
    ui::PresentationWatcher watcher(missing);
    REQUIRE(watcher.revision() == 0);
    REQUIRE(watcher.spec().name == "bar");
    REQUIRE(watcher.poll(1.0) == false);
    REQUIRE(watcher.spec().name == "bar");
    REQUIRE(watcher.error().find("not found") != std::string::npos);
}

TEST_CASE("max_lines truncates an element", "[presentation][layout]") {
    ui::PresentationSpec spec = ui::PresentationSpec::bar();
    auto* target = const_cast<ui::ElementSpec*>(spec.find("target"));
    target->max_lines = 1;
    ui::LayoutRequest request;
    request.text.target = "first\nsecond\nthird";
    request.work_area = CaptureRect{0, 0, 1920, 1080};
    request.dpi = 96;
    request.spec = spec;
    const ui::Layout layout = ui::compute_layout(request, fake_measure());
    REQUIRE(layout.target_lines == 1);
    REQUIRE(layout.runs.back().text == "first");
}

TEST_CASE("elements stack by their order field", "[presentation][layout]") {
    ui::PresentationSpec spec = ui::PresentationSpec::bar();
    auto* target = const_cast<ui::ElementSpec*>(spec.find("target"));
    auto* source = const_cast<ui::ElementSpec*>(spec.find("source"));
    target->order = 0;
    source->order = 1;
    ui::LayoutRequest request;
    request.text.source = "source";
    request.text.target = "target";
    request.work_area = CaptureRect{0, 0, 1920, 1080};
    request.dpi = 96;
    request.spec = spec;
    const ui::Layout layout = ui::compute_layout(request, fake_measure());
    REQUIRE(layout.runs.size() == 2);
    REQUIRE(layout.runs[0].kind == ui::RunKind::kTarget);
    REQUIRE(layout.runs[1].kind == ui::RunKind::kSource);
}

TEST_CASE("low confidence dims the target to dim_opacity", "[presentation][layout]") {
    ui::PresentationSpec spec = ui::PresentationSpec::bar();
    spec.confidence.dim_below = 0.5;
    spec.confidence.dim_opacity = 0.4;
    ui::LayoutRequest request;
    request.text.target = "target";
    request.text.confidence = 0.3;
    request.work_area = CaptureRect{0, 0, 1920, 1080};
    request.dpi = 96;
    request.spec = spec;
    const auto dimmed = ui::compute_layout(request, fake_measure());
    REQUIRE(dimmed.runs.back().style.opacity == Catch::Approx(0.4));

    request.text.confidence = 0.9;
    const auto bright = ui::compute_layout(request, fake_measure());
    REQUIRE(bright.runs.back().style.opacity == Catch::Approx(1.0));
}

TEST_CASE("low coverage dims the target like the prototype", "[presentation][layout]") {
    ui::PresentationSpec spec = ui::PresentationSpec::bar();
    spec.confidence.dim_below = 0.5;
    spec.confidence.dim_opacity = 0.4;
    ui::LayoutRequest request;
    request.text.target = "target";
    request.text.confidence = 1.0;
    request.text.coverage = 0.2;
    request.work_area = CaptureRect{0, 0, 1920, 1080};
    request.dpi = 96;
    request.spec = spec;
    const ui::Layout layout = ui::compute_layout(request, fake_measure());
    REQUIRE(layout.runs.back().style.opacity == Catch::Approx(0.4));
}

TEST_CASE("anchors place the block where the spec says", "[presentation][anchor]") {
    const CaptureRect work{0, 0, 1920, 1080};
    const ui::Point top_left =
        ui::anchor_position(ui::Anchor::kTopLeft, work, 400, 100, 24, 18);
    REQUIRE(top_left.x == 24);
    REQUIRE(top_left.y == 18);

    const ui::Point top_center =
        ui::anchor_position(ui::Anchor::kTopCenter, work, 400, 100, 0, 0);
    REQUIRE(top_center.x == 760);
    REQUIRE(top_center.y == 0);

    const ui::Point middle_right =
        ui::anchor_position(ui::Anchor::kMiddleRight, work, 400, 100, 0, 0);
    REQUIRE(middle_right.x == 1520);
    REQUIRE(middle_right.y == 490);

    const ui::Point bottom_right =
        ui::anchor_position(ui::Anchor::kBottomRight, work, 400, 100, -20, -30);
    REQUIRE(bottom_right.x == 1500);
    REQUIRE(bottom_right.y == 950);
}

TEST_CASE("an oversized offset is clamped inside the work area", "[presentation][anchor]") {
    const CaptureRect work{0, 0, 1920, 1080};
    const ui::Point outside =
        ui::anchor_position(ui::Anchor::kTopLeft, work, 400, 100, -50, -60);
    REQUIRE(outside.x == 0);
    REQUIRE(outside.y == 0);

    const ui::Point past_right =
        ui::anchor_position(ui::Anchor::kTopLeft, work, 400, 100, 5000, 5000);
    REQUIRE(past_right.x == 1520);
    REQUIRE(past_right.y == 980);
}

TEST_CASE("anchoring is exact at 96, 120 and 144 dpi", "[presentation][anchor]") {
    const CaptureRect work{0, 0, 2560, 1600};
    for (int dpi : {96, 120, 144}) {
        const int width = ui::logical_px(400, dpi);
        const int height = ui::logical_px(100, dpi);
        const ui::Point placed = ui::anchor_position(
            ui::Anchor::kBottomCenter,
            work,
            width,
            height,
            ui::logical_px(0, dpi),
            ui::logical_px(-90, dpi));
        REQUIRE(placed.x == (2560 - width) / 2);
        REQUIRE(placed.y == 1600 - height - ui::logical_px(90, dpi));
    }
}

TEST_CASE("max_width_ratio caps the plate and wraps long text", "[presentation][layout]") {
    ui::PresentationSpec spec = ui::PresentationSpec::bar();
    spec.layout.max_width_ratio = 0.25;
    ui::LayoutRequest request;
    request.text.target =
        "the sword intent of this sect is a myth and the realm behind it";
    request.work_area = CaptureRect{0, 0, 800, 600};
    request.dpi = 96;
    request.spec = spec;
    const ui::Layout layout = ui::compute_layout(request, fake_measure());
    REQUIRE(layout.width <= 200);
    REQUIRE(layout.target_lines >= 2);
}

TEST_CASE("line_gap changes the block height", "[presentation][layout]") {
    const ui::TextMeasure measure = fake_measure();
    ui::PresentationSpec tight = ui::PresentationSpec::bar();
    tight.layout.line_gap = 2;
    ui::PresentationSpec loose = ui::PresentationSpec::bar();
    loose.layout.line_gap = 20;

    ui::LayoutRequest request;
    request.text.source = "line one\nline two";
    request.work_area = CaptureRect{0, 0, 1920, 1080};
    request.dpi = 96;
    request.spec = tight;
    const ui::Layout tight_layout = ui::compute_layout(request, measure);
    request.spec = loose;
    const ui::Layout loose_layout = ui::compute_layout(request, measure);
    REQUIRE(loose_layout.height - tight_layout.height == 18);
}

TEST_CASE("align moves a short line inside the content width", "[presentation][layout]") {
    const ui::TextMeasure measure = fake_measure();
    ui::PresentationSpec spec = ui::PresentationSpec::bar();
    spec.layout.align = ui::Align::kCenter;
    ui::LayoutRequest request;
    request.text.source = "s";
    request.text.target = "a much longer target line";
    request.work_area = CaptureRect{0, 0, 1920, 1080};
    request.dpi = 96;
    request.spec = spec;
    const ui::Layout center = ui::compute_layout(request, measure);

    const auto* source_run = &center.runs.front();
    REQUIRE(source_run->kind == ui::RunKind::kSource);
    const int padding = ui::logical_px(12, 96);
    REQUIRE(source_run->x >
            padding + (source_run->width / 2));

    spec.layout.align = ui::Align::kLeft;
    request.spec = spec;
    const ui::Layout left = ui::compute_layout(request, measure);
    REQUIRE(left.runs.front().x == padding);
}

TEST_CASE("background none leaves the plate transparent", "[presentation][layout]") {
    ui::PresentationSpec spec = ui::PresentationSpec::bar();
    spec.background.kind = ui::BackgroundKind::kNone;
    ui::LayoutRequest request;
    request.text.target = "target";
    request.work_area = CaptureRect{0, 0, 1920, 1080};
    request.dpi = 96;
    request.spec = spec;
    const ui::Layout layout = ui::compute_layout(request, fake_measure());
    REQUIRE_FALSE(layout.background.plate);
}

TEST_CASE("hidden mode draws nothing", "[presentation][layout]") {
    ui::PresentationSpec spec = ui::PresentationSpec::bar();
    spec.layout.mode = ui::LayoutMode::kHidden;
    ui::LayoutRequest request;
    request.text.target = "target";
    request.text.status = "live";
    request.work_area = CaptureRect{0, 0, 1920, 1080};
    request.dpi = 96;
    request.spec = spec;
    const ui::Layout layout = ui::compute_layout(request, fake_measure());
    REQUIRE(layout.hidden);
    REQUIRE(layout.empty);
    REQUIRE(layout.runs.empty());
}

TEST_CASE("element colours and opacity reach the run style", "[presentation][layout]") {
    ui::PresentationSpec spec = ui::PresentationSpec::bar();
    auto* target = const_cast<ui::ElementSpec*>(spec.find("target"));
    target->color = ui::Rgb{0x11, 0x22, 0x33};
    target->opacity = 0.5;
    target->has_outline = true;
    target->outline = ui::OutlineSpec{ui::Rgb{0, 0, 0}, 2};
    ui::LayoutRequest request;
    request.text.target = "target";
    request.work_area = CaptureRect{0, 0, 1920, 1080};
    request.dpi = 144;
    request.spec = spec;
    const ui::Layout layout = ui::compute_layout(request, fake_measure());
    const auto& run = layout.runs.back();
    REQUIRE(run.style.color.r == 0x11);
    REQUIRE(run.style.color.g == 0x22);
    REQUIRE(run.style.color.b == 0x33);
    REQUIRE(run.style.opacity == Catch::Approx(0.5));
    REQUIRE(run.style.outline_px == ui::logical_px(2, 144));
}

TEST_CASE("the renderer paints the outline and the fill", "[presentation][render]") {
    ui::PresentationSpec spec = ui::PresentationSpec::bar();
    spec.background.kind = ui::BackgroundKind::kNone;
    auto* target = const_cast<ui::ElementSpec*>(spec.find("target"));
    target->has_outline = true;
    target->outline = ui::OutlineSpec{ui::Rgb{0, 0, 0}, 2};
    target->color = ui::Rgb{255, 255, 255};

    ui::GdiText text;
    ui::LayoutRequest request;
    request.text.target = "target";
    request.work_area = CaptureRect{0, 0, 1920, 1080};
    request.dpi = 96;
    request.spec = spec;
    const ui::Layout layout = ui::compute_layout(request, text.as_measure());
    const ui::OverlayImage image = ui::compose_overlay(layout, text);
    REQUIRE_FALSE(image.empty());

    bool white = false;
    bool black = false;
    for (std::size_t index = 0; index < image.bgra.size(); index += 4) {
        const std::uint8_t alpha = image.bgra[index + 3];
        const std::uint8_t blue = image.bgra[index + 0];
        const std::uint8_t green = image.bgra[index + 1];
        const std::uint8_t red = image.bgra[index + 2];
        if (alpha > 0 && blue < 40 && green < 40 && red < 40) {
            black = true;
        }
        if (alpha > 100 && blue > 150 && green > 150 && red > 150) {
            white = true;
        }
    }
    REQUIRE(black);
    REQUIRE(white);
}

TEST_CASE("a transparent background leaves the corners empty", "[presentation][render]") {
    ui::PresentationSpec spec = ui::PresentationSpec::bar();
    spec.background.kind = ui::BackgroundKind::kNone;
    ui::GdiText text;
    ui::LayoutRequest request;
    request.text.target = "target";
    request.work_area = CaptureRect{0, 0, 1920, 1080};
    request.dpi = 96;
    request.spec = spec;
    const ui::Layout layout = ui::compute_layout(request, text.as_measure());
    const ui::OverlayImage image = ui::compose_overlay(layout, text);
    REQUIRE_FALSE(image.empty());
    REQUIRE(image.bgra[3] == 0);
    REQUIRE(image.bgra[static_cast<std::size_t>(image.width - 1) * 4 + 3] == 0);
}

TEST_CASE("element opacity dims the composited alpha", "[presentation][render]") {
    ui::GdiText text;
    ui::LayoutRequest request;
    request.text.target = "target";
    request.work_area = CaptureRect{0, 0, 1920, 1080};
    request.dpi = 96;

    ui::PresentationSpec opaque = ui::PresentationSpec::bar();
    request.spec = opaque;
    const ui::Layout opaque_layout = ui::compute_layout(request, text.as_measure());
    const ui::OverlayImage opaque_image = ui::compose_overlay(opaque_layout, text);

    ui::PresentationSpec dimmed = ui::PresentationSpec::bar();
    const_cast<ui::ElementSpec*>(dimmed.find("target"))->opacity = 0.4;
    request.spec = dimmed;
    const ui::Layout dim_layout = ui::compute_layout(request, text.as_measure());
    const ui::OverlayImage dim_image = ui::compose_overlay(dim_layout, text);

    std::uint8_t opaque_max = 0;
    std::uint8_t dim_max = 0;
    for (std::size_t index = 3; index < opaque_image.bgra.size(); index += 4) {
        opaque_max = std::max(opaque_max, opaque_image.bgra[index]);
    }
    for (std::size_t index = 3; index < dim_image.bgra.size(); index += 4) {
        dim_max = std::max(dim_max, dim_image.bgra[index]);
    }
    // The dimmed text must sit clearly below a fully opaque white glyph.
    REQUIRE(dim_max < opaque_max);
}
