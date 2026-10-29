#include "watashi/capture.hpp"

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdint>

namespace watashi {
namespace {

double steady_seconds() {
    return std::chrono::duration<double>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

}  // namespace

SyntheticCapture::SyntheticCapture(Options options) : options_(options) {
    if (options_.width <= 0) {
        options_.width = 1200;
    }
    if (options_.height <= 0) {
        options_.height = 200;
    }
}

CaptureFrame SyntheticCapture::grab() {
    CaptureFrame frame;
    frame.t_start_s = steady_seconds();
    frame.width = options_.width;
    frame.height = options_.height;
    frame.origin_x = 0;
    frame.origin_y = 0;

    const unsigned char background = 18;  // matches the subtitle fixture's card
    frame.bgr.assign(
        static_cast<std::size_t>(frame.width) * static_cast<std::size_t>(frame.height) * 3,
        background);

    if (options_.animate) {
        // A white block one fifth of the width, sweeping left to right and
        // wrapping. Enough motion for the change detector, nothing else.
        const int block_width = std::max(1, frame.width / 5);
        const int block_height = std::max(1, frame.height / 3);
        const int span = std::max(1, frame.width - block_width);
        const int left = (tick_ * std::max(1, span / 40)) % span;
        const int top = (frame.height - block_height) / 2;
        for (int y = top; y < top + block_height; ++y) {
            std::uint8_t* row =
                frame.bgr.data() + static_cast<std::size_t>(y) * static_cast<std::size_t>(frame.width) * 3;
            for (int x = left; x < left + block_width; ++x) {
                std::uint8_t* pixel = row + static_cast<std::size_t>(x) * 3;
                pixel[0] = 235;
                pixel[1] = 235;
                pixel[2] = 235;
            }
        }
        ++tick_;
    }

    frame.t_end_s = steady_seconds();
    return frame;
}

CaptureInfo SyntheticCapture::info() const {
    return CaptureInfo{
        "synthetic",
        CaptureRect{0, 0, options_.width, options_.height},
        "synthetic " + std::to_string(options_.width) + "x" + std::to_string(options_.height),
    };
}

}  // namespace watashi
