#include <catch2/catch_test_macros.hpp>

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "watashi/capture.hpp"
#include "watashi/pipeline.hpp"
#include "watashi/session.hpp"

namespace {

/// A frame of one colour, so a test can state exactly what the detector sees.
std::vector<std::uint8_t> solid(int width, int height, std::uint8_t b, std::uint8_t g,
                                std::uint8_t r) {
    std::vector<std::uint8_t> bytes(
        static_cast<std::size_t>(width) * static_cast<std::size_t>(height) * 3);
    for (std::size_t index = 0; index < bytes.size(); index += 3) {
        bytes[index] = b;
        bytes[index + 1] = g;
        bytes[index + 2] = r;
    }
    return bytes;
}

void paint(std::vector<std::uint8_t>& bytes, int width, int left, int top, int block_width,
           int block_height, std::uint8_t value) {
    for (int y = top; y < top + block_height; ++y) {
        for (int x = left; x < left + block_width; ++x) {
            std::size_t offset =
                (static_cast<std::size_t>(y) * static_cast<std::size_t>(width) +
                 static_cast<std::size_t>(x)) *
                3;
            bytes[offset] = value;
            bytes[offset + 1] = value;
            bytes[offset + 2] = value;
        }
    }
}

struct CaptureRun {
    std::vector<watashi::Envelope> events;
    nlohmann::json last_stats;
};

CaptureRun run_capture_session(watashi::CaptureSource& source, double duration, double fps,
                               watashi::DiffOptions diff) {
    watashi::FakeOcr ocr;
    watashi::FakeTranslator translator;
    watashi::SessionOptions options{duration, fps};
    options.capture = &source;
    options.diff = diff;
    watashi::Session session(options, ocr, translator);

    CaptureRun run;
    session.run([&run](const watashi::Envelope& envelope) {
        if (envelope.type == "stats") {
            run.last_stats = envelope.data;
        }
        run.events.push_back(envelope);
    });
    return run;
}

}  // namespace

TEST_CASE("make_rect keeps right and bottom exclusive") {
    const auto rect = watashi::capture_detail::make_rect(-1920, 100, -1020, 400);
    REQUIRE(rect.x == -1920);
    REQUIRE(rect.y == 100);
    REQUIRE(rect.width == 900);
    REQUIRE(rect.height == 300);
    REQUIRE(rect.right() == -1020);
    REQUIRE(rect.bottom() == 400);
    REQUIRE_FALSE(watashi::capture_detail::rect_empty(rect));
}

TEST_CASE("a rectangle fully inside the bounds is unchanged") {
    const watashi::CaptureRect wanted{60, 60, 1200, 200};
    const watashi::CaptureRect bounds{0, 0, 1707, 1067};
    const auto clipped = watashi::capture_detail::clip_rect(wanted, bounds);
    REQUIRE(clipped.x == 60);
    REQUIRE(clipped.y == 60);
    REQUIRE(clipped.width == 1200);
    REQUIRE(clipped.height == 200);
}

TEST_CASE("a rectangle crossing the right edge is clipped, not moved") {
    const watashi::CaptureRect wanted{1600, 60, 1200, 200};
    const watashi::CaptureRect bounds{0, 0, 1707, 1067};
    const auto clipped = watashi::capture_detail::clip_rect(wanted, bounds);
    REQUIRE(clipped.x == 1600);
    REQUIRE(clipped.y == 60);
    REQUIRE(clipped.width == 107);
    REQUIRE(clipped.height == 200);
}

TEST_CASE("a region outside the screen clips to empty") {
    const watashi::CaptureRect wanted{5000, 5000, 100, 100};
    const watashi::CaptureRect bounds{0, 0, 1707, 1067};
    REQUIRE(watashi::capture_detail::rect_empty(
        watashi::capture_detail::clip_rect(wanted, bounds)));
}

TEST_CASE("negative coordinates of a left-hand monitor clip against the virtual screen") {
    // Second monitor to the left of the primary: x from -1920 to 0.
    const watashi::CaptureRect virtual_screen{-1920, 0, 3627, 1080};
    const watashi::CaptureRect wanted{-2000, 10, 200, 100};
    const auto clipped = watashi::capture_detail::clip_rect(wanted, virtual_screen);
    REQUIRE(clipped.x == -1920);
    REQUIRE(clipped.y == 10);
    REQUIRE(clipped.width == 120);
    REQUIRE(clipped.height == 100);
}

TEST_CASE("logical to physical scaling rounds each edge once") {
    const watashi::CaptureRect logical{100, 50, 300, 200};
    const auto physical = watashi::capture_detail::scale_rect(logical, 1.5);
    REQUIRE(physical.x == 150);
    REQUIRE(physical.y == 75);
    REQUIRE(physical.width == 450);
    REQUIRE(physical.height == 300);

    const auto at_100 = watashi::capture_detail::scale_rect(logical, 1.0);
    REQUIRE(at_100.width == 300);
}

TEST_CASE("dpi_scale maps 96 dpi to 1.0 and clamps nonsense") {
    REQUIRE(watashi::capture_detail::dpi_scale(96) == 1.0);
    REQUIRE(watashi::capture_detail::dpi_scale(144) == 1.5);
    REQUIRE(watashi::capture_detail::dpi_scale(120) == 1.25);
    REQUIRE(watashi::capture_detail::dpi_scale(0) == 1.0);
    REQUIRE(watashi::capture_detail::dpi_scale(-96) == 1.0);
}

TEST_CASE("the first frame always counts as changed") {
    watashi::ChangeDetector detector({0.02, 64, 36});
    const auto bytes = solid(120, 20, 18, 18, 18);
    REQUIRE(detector.changed(watashi::ImageView{bytes.data(), 120, 20}));
}

TEST_CASE("an identical frame does not count as changed") {
    watashi::ChangeDetector detector({0.02, 64, 36});
    const auto bytes = solid(120, 20, 18, 18, 18);
    REQUIRE(detector.changed(watashi::ImageView{bytes.data(), 120, 20}));
    REQUIRE_FALSE(detector.changed(watashi::ImageView{bytes.data(), 120, 20}));
    REQUIRE(detector.last_difference() == 0.0);
}

TEST_CASE("a large changed block counts as changed") {
    watashi::ChangeDetector detector({0.02, 64, 36});
    auto first = solid(120, 20, 18, 18, 18);
    REQUIRE(detector.changed(watashi::ImageView{first.data(), 120, 20}));

    paint(first, 120, 10, 2, 40, 10, 235);
    REQUIRE(detector.changed(watashi::ImageView{first.data(), 120, 20}));
    REQUIRE(detector.last_difference() > 0.02);
}

TEST_CASE("a change below the threshold is ignored") {
    watashi::ChangeDetector detector({0.9, 64, 36});
    auto first = solid(120, 20, 18, 18, 18);
    REQUIRE(detector.changed(watashi::ImageView{first.data(), 120, 20}));

    // One bright column on a dark field: a real difference, but a small one.
    paint(first, 120, 0, 0, 2, 20, 255);
    REQUIRE_FALSE(detector.changed(watashi::ImageView{first.data(), 120, 20}));
}

TEST_CASE("a zero threshold disables skipping") {
    watashi::ChangeDetector detector({0.0, 64, 36});
    const auto bytes = solid(120, 20, 18, 18, 18);
    REQUIRE(detector.changed(watashi::ImageView{bytes.data(), 120, 20}));
    REQUIRE(detector.changed(watashi::ImageView{bytes.data(), 120, 20}));
    REQUIRE_FALSE(detector.enabled());
}

TEST_CASE("gray_grid has one value in [0, 1] per cell") {
    const auto bytes = solid(120, 20, 100, 100, 100);
    const auto grid = watashi::capture_detail::gray_grid(
        watashi::ImageView{bytes.data(), 120, 20}, 8, 4);
    REQUIRE(grid.size() == 32);
    for (float value : grid) {
        REQUIRE(value >= 0.0f);
        REQUIRE(value <= 1.0f);
        REQUIRE(value > 0.3f);
    }
}

TEST_CASE("synthetic capture reports its region and produces the same bytes twice") {
    watashi::SyntheticCapture source({120, 20, false});
    const auto info = source.info();
    REQUIRE(info.mode == "synthetic");
    REQUIRE(info.region.width == 120);
    REQUIRE(info.region.height == 20);

    const auto first = source.grab();
    const auto second = source.grab();
    REQUIRE(first.width == 120);
    REQUIRE(first.height == 20);
    REQUIRE(first.bgr.size() == 120u * 20u * 3u);
    REQUIRE(first.bgr == second.bgr);
    REQUIRE(first.t_end_s >= first.t_start_s);
}

TEST_CASE("animated synthetic capture changes between frames") {
    watashi::SyntheticCapture source({120, 20, true});
    watashi::ChangeDetector detector({0.001, 64, 36});
    REQUIRE(detector.changed(source.grab().view()));
    REQUIRE(detector.changed(source.grab().view()));
}

TEST_CASE("a still capture run calls OCR once and reports the skips") {
    watashi::SyntheticCapture source({120, 20, false});
    const auto run = run_capture_session(
        source, 0.2, 100.0, watashi::DiffOptions{0.02, 64, 36});

    REQUIRE(run.last_stats.at("frames") == 20);
    REQUIRE(run.last_stats.at("ocr_calls") == 1);
    REQUIRE(run.last_stats.at("skipped") == 19);
    REQUIRE(run.last_stats.at("capture_ms").get<double>() >= 0.0);
}

TEST_CASE("a moving capture run keeps calling OCR") {
    watashi::SyntheticCapture source({120, 20, true});
    const auto run = run_capture_session(
        source, 0.2, 100.0, watashi::DiffOptions{0.001, 64, 36});

    REQUIRE(run.last_stats.at("frames") == 20);
    REQUIRE(run.last_stats.at("ocr_calls") == 20);
    REQUIRE(run.last_stats.at("skipped") == 0);
}

TEST_CASE("capture events keep the contract order and carry subtitles on skipped frames") {
    watashi::SyntheticCapture source({120, 20, false});
    const auto run = run_capture_session(
        source, 0.05, 40.0, watashi::DiffOptions{0.02, 64, 36});

    REQUIRE(run.events.size() == 1 + 2 + 2 + 1);  // ready, 2 x (subtitle, stats), stopped
    REQUIRE(run.events.front().type == "ready");
    REQUIRE(run.events.back().type == "stopped");
    for (std::size_t index = 1; index < run.events.size(); ++index) {
        REQUIRE(run.events[index].seq == run.events[index - 1].seq + 1);
    }

    const auto subtitles = std::count_if(
        run.events.begin(), run.events.end(),
        [](const watashi::Envelope& event) { return event.type == "subtitle"; });
    REQUIRE(subtitles == 2);
    for (const auto& event : run.events) {
        if (event.type == "subtitle") {
            REQUIRE(event.data.at("lines").size() == 2);
        }
    }
}

TEST_CASE("a capture ready event names the source and its rectangle") {
    watashi::SyntheticCapture source({120, 20, false});
    const auto run = run_capture_session(
        source, 0.05, 40.0, watashi::DiffOptions{0.02, 64, 36});

    const auto& ready = run.events.front().data;
    REQUIRE(ready.at("mode") == "synthetic");
    REQUIRE(ready.at("capture_mode") == "synthetic");
    REQUIRE(ready.at("region") == "synthetic 120x20");
    REQUIRE(ready.at("region_box") == nlohmann::json::array({0, 0, 120, 20}));
    REQUIRE(ready.at("diff_threshold") == 0.02);
}

TEST_CASE("diff disabled means every capture frame is read") {
    watashi::SyntheticCapture source({120, 20, false});
    const auto run = run_capture_session(source, 0.05, 40.0, watashi::DiffOptions{});

    REQUIRE(run.last_stats.at("frames") == 2);
    REQUIRE(run.last_stats.at("ocr_calls") == 2);
    REQUIRE(run.last_stats.at("skipped") == 0);
}

namespace {

/// Checks the bytes OCR is handed, not just that it was called: a frame that
/// goes out of scope before translation would make this fail.
class PixelInspector final : public watashi::OcrSource {
public:
    [[nodiscard]] watashi::OcrResult recognize(const watashi::ImageView& frame) const override {
        calls += 1;
        width = frame.width;
        height = frame.height;
        if (!frame.empty()) {
            first_pixel = frame.bgr[0];
        }
        return {};
    }

    mutable int calls = 0;
    mutable int width = 0;
    mutable int height = 0;
    mutable std::uint8_t first_pixel = 0;
};

}  // namespace

TEST_CASE("OCR reads the captured pixels of the frame it was handed") {
    watashi::SyntheticCapture source({120, 20, false});
    PixelInspector ocr;
    watashi::FakeTranslator translator;
    watashi::SessionOptions options{0.05, 40.0};
    options.capture = &source;
    options.diff = watashi::DiffOptions{0.02, 64, 36};
    watashi::Session session(options, ocr, translator);

    std::vector<watashi::Envelope> events;
    session.run([&events](const watashi::Envelope& envelope) {
        events.push_back(envelope);
    });

    REQUIRE(ocr.calls == 1);
    REQUIRE(ocr.width == 120);
    REQUIRE(ocr.height == 20);
    REQUIRE(ocr.first_pixel == 18);
}
