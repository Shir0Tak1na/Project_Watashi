#include <catch2/catch_test_macros.hpp>

#include <cstdint>
#include <string>
#include <vector>

#include "watashi/ocr.hpp"

namespace {

watashi::ImageView blank_frame(int width, int height, std::vector<std::uint8_t>& storage) {
    storage.assign(static_cast<std::size_t>(width) * height * 3, 128);
    return watashi::ImageView{storage.data(), width, height};
}

std::vector<float> probability_map(int width, int height) {
    return std::vector<float>(static_cast<std::size_t>(width) * height, 0.0F);
}

std::vector<float> logits_for(const std::vector<int>& best, int classes) {
    std::vector<float> logits(static_cast<std::size_t>(best.size()) * classes, 0.01F);
    for (std::size_t step = 0; step < best.size(); ++step) {
        logits[step * static_cast<std::size_t>(classes) + static_cast<std::size_t>(best[step])] =
            0.9F;
    }
    return logits;
}

}  // namespace

TEST_CASE("detector preprocessing rounds both sides to a multiple of 32") {
    std::vector<std::uint8_t> storage;
    const auto frame = blank_frame(1200, 200, storage);

    const auto input = watashi::ocr_detail::prepare_det(frame);

    REQUIRE(input.width == 1216);
    REQUIRE(input.height == 192);
    REQUIRE(input.data.size() == static_cast<std::size_t>(3) * 1216 * 192);
    for (const float value : input.data) {
        REQUIRE(value >= -1.0F);
        REQUIRE(value <= 1.0F);
    }
}

TEST_CASE("small frames keep the 32-pixel minimum and stay native") {
    std::vector<std::uint8_t> storage;
    const auto frame = blank_frame(100, 40, storage);

    const auto input = watashi::ocr_detail::prepare_det(frame);

    REQUIRE(input.width == 96);
    REQUIRE(input.height == 32);
}

TEST_CASE("an empty frame is rejected before any arithmetic") {
    const watashi::ImageView empty{nullptr, 0, 0};

    REQUIRE_THROWS_AS(
        watashi::ocr_detail::prepare_det(empty),
        watashi::OcrError);

    std::vector<std::uint8_t> storage;
    const auto null_pixels = blank_frame(10, 10, storage);
    const watashi::ImageView broken{nullptr, null_pixels.width, null_pixels.height};
    REQUIRE_THROWS_AS(watashi::ocr_detail::prepare_det(broken), watashi::OcrError);
}

TEST_CASE("db post-processing turns a filled rectangle into one box") {
    constexpr int width = 200;
    constexpr int height = 60;
    std::vector<float> probabilities = probability_map(width, height);
    for (int y = 20; y < 30; ++y) {
        for (int x = 40; x < 90; ++x) {
            probabilities[static_cast<std::size_t>(y) * width + x] = 0.9F;
        }
    }

    const auto boxes = watashi::ocr_detail::db_boxes(probabilities, width, height, width, height);

    REQUIRE(boxes.size() == 1);
    REQUIRE(boxes[0].x < 40);
    REQUIRE(boxes[0].x + boxes[0].width > 90);
    REQUIRE(boxes[0].height > 10);
    REQUIRE(boxes[0].x >= 0);
    REQUIRE(boxes[0].x + boxes[0].width <= width);
}

TEST_CASE("db post-processing finds nothing on an all-zero map") {
    constexpr int width = 40;
    constexpr int height = 40;
    const auto probabilities = probability_map(width, height);

    REQUIRE(watashi::ocr_detail::db_boxes(probabilities, width, height, width, height).empty());
}

TEST_CASE("db post-processing rejects a buffer that does not match its shape") {
    const std::vector<float> probabilities(10, 0.9F);

    REQUIRE_THROWS_AS(
        watashi::ocr_detail::db_boxes(probabilities, 20, 20, 20, 20),
        watashi::OcrError);
}

TEST_CASE("db post-processing rejects a source frame with no size") {
    constexpr int width = 20;
    constexpr int height = 20;
    const auto probabilities = probability_map(width, height);

    REQUIRE_THROWS_AS(
        watashi::ocr_detail::db_boxes(probabilities, width, height, 0, 20),
        watashi::OcrError);
}

TEST_CASE("ctc decoding collapses repeats and drops blanks") {
    const std::vector<std::string> charset = {"blank", "a", "b"};
    // blank, a, a, blank, b -> "ab" (repeats collapse, blanks vanish)
    const std::vector<float> logits = logits_for({0, 1, 1, 0, 2}, 3);
    double confidence = 0.0;

    const std::string text =
        watashi::ocr_detail::ctc_decode(logits, 5, 3, charset, confidence);

    REQUIRE(text == "ab");
    REQUIRE(confidence > 0.8);
}

TEST_CASE("ctc decoding all blanks yields no text and no confidence") {
    const std::vector<std::string> charset = {"blank", "a"};
    const std::vector<float> logits = logits_for({0, 0, 0}, 2);
    double confidence = 0.5;

    const std::string text =
        watashi::ocr_detail::ctc_decode(logits, 3, 2, charset, confidence);

    REQUIRE(text.empty());
    REQUIRE(confidence == 0.0);
}

TEST_CASE("ctc decoding rejects a buffer that does not match its shape") {
    const std::vector<std::string> charset = {"blank", "a"};
    const std::vector<float> logits(4, 0.9F);
    double confidence = 0.0;

    REQUIRE_THROWS_AS(
        watashi::ocr_detail::ctc_decode(logits, 3, 2, charset, confidence),
        watashi::OcrError);
}

TEST_CASE("onnx ocr refuses an incomplete configuration") {
    watashi::OnnxOcr::Options options;

    REQUIRE_THROWS_AS(watashi::OnnxOcr(options), watashi::OcrError);

    options.det_model = "does-not-exist-det.onnx";
    options.rec_model = "does-not-exist-rec.onnx";
    options.charset_file = "does-not-exist-chars.txt";
    REQUIRE_THROWS_AS(watashi::OnnxOcr(options), watashi::OcrError);
}
