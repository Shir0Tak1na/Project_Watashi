// OCR is a source of lines, not a screen and not a model: the same interface
// serves the deterministic fake used by the session/CLI default and the ONNX
// Runtime implementation used with --ocr onnx.
//
// Pixels cross this boundary as a raw BGR8 buffer in the contract's channel
// order. No image decoding library is involved on purpose: whatever produced
// the frame (capture, a test fixture, a file) hands over bytes it already has.

#pragma once

#include <cstdint>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace watashi {

/// Screen geometry in region-relative pixels. The wire form is the corner pair
/// ``[x, y, x + width, y + height]`` that the Python contract publishes.
struct Box {
    int x = 0;
    int y = 0;
    int width = 0;
    int height = 0;

    [[nodiscard]] nlohmann::json to_json() const;
};

/// One recognised line, before translation.
struct OcrLine {
    std::string text;
    double confidence = 1.0;
    std::optional<Box> box;
};

/// Everything one frame produced, plus what it cost.
struct OcrResult {
    std::vector<OcrLine> lines;
    double elapsed_ms = 0.0;
};

/// A borrowed view of a frame. ``bgr`` is ``width * height * 3`` bytes,
/// row-major, no padding, channels in B, G, R order.
struct ImageView {
    const std::uint8_t* bgr = nullptr;
    int width = 0;
    int height = 0;

    [[nodiscard]] bool empty() const noexcept {
        return bgr == nullptr || width <= 0 || height <= 0;
    }
};

/// A missing model, an unreadable character table, a malformed frame buffer.
class OcrError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

/// The seam every recogniser implements.
class OcrSource {
public:
    virtual ~OcrSource() = default;
    [[nodiscard]] virtual OcrResult recognize(const ImageView& frame) const = 0;
};

/// Pure arithmetic used by the ONNX implementation, exposed so the parts that
/// need no model can be tested without one.
namespace ocr_detail {

struct DetInput {
    std::vector<float> data;  // CHW, normalised to [-1, 1]
    int width = 0;
    int height = 0;
};

/// Resize to a multiple of 32 with the long side capped, then normalise.
/// Resizing a 1200x200 frame yields 1216x192, which is what the detector sees.
[[nodiscard]] DetInput prepare_det(const ImageView& frame, int limit_side_len = 1500);

/// Threshold, 2x2 dilate, 8-connected components, minimum-area unclip.
/// ``probabilities`` is the detector's HxW output; returns boxes in source
/// coordinates. Throws OcrError when the buffer does not match H*W.
[[nodiscard]] std::vector<Box> db_boxes(
    const std::vector<float>& probabilities,
    int width,
    int height,
    int source_width,
    int source_height);

/// CTC greedy decode: argmax per step, collapse repeats, drop blank (index 0).
/// ``confidence`` is the mean probability of the characters kept, 0 when none.
[[nodiscard]] std::string ctc_decode(
    const std::vector<float>& logits,
    int steps,
    int classes,
    const std::vector<std::string>& charset,
    double& confidence);

}  // namespace ocr_detail

/// PaddleOCR PP-OCRv4 det + optional cls + rec, through ONNX Runtime.
class OnnxOcr final : public OcrSource {
public:
    struct Options {
        std::string det_model;
        std::string rec_model;
        /// Optional; rotating an upside-down crop is the only thing it is for.
        std::string cls_model;
        /// Character table: one UTF-8 character per line, as the rec model's
        /// metadata publishes it.
        std::string charset_file;
        int intra_threads = 4;
        int inter_threads = 1;
        bool use_memory_arena = true;
    };

    /// Loads the models now, so a bad path fails at construction rather than on
    /// the first frame. Throws OcrError.
    explicit OnnxOcr(Options options);
    ~OnnxOcr() override;

    OnnxOcr(const OnnxOcr&) = delete;
    OnnxOcr& operator=(const OnnxOcr&) = delete;
    OnnxOcr(OnnxOcr&&) noexcept;
    OnnxOcr& operator=(OnnxOcr&&) noexcept;

    [[nodiscard]] OcrResult recognize(const ImageView& frame) const override;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace watashi
