#include "watashi/capture.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>

namespace watashi {

namespace capture_detail {

std::vector<float> gray_grid(const ImageView& frame, int grid_width, int grid_height) {
    if (frame.empty() || grid_width <= 0 || grid_height <= 0) {
        return {};
    }

    const std::size_t cells =
        static_cast<std::size_t>(grid_width) * static_cast<std::size_t>(grid_height);
    std::vector<float> grid(cells, 0.0f);
    std::vector<int> counts(cells, 0);

    for (int y = 0; y < frame.height; ++y) {
        // Every pixel belongs to exactly one cell; the last row/column of cells
        // absorbs the remainder so a 1200-wide frame at 64 cells stays covered.
        const int gy = std::min(grid_height - 1, y * grid_height / frame.height);
        const std::uint8_t* row =
            frame.bgr + static_cast<std::size_t>(y) * static_cast<std::size_t>(frame.width) * 3;
        for (int x = 0; x < frame.width; ++x) {
            const int gx = std::min(grid_width - 1, x * grid_width / frame.width);
            const std::size_t cell =
                static_cast<std::size_t>(gy) * static_cast<std::size_t>(grid_width) +
                static_cast<std::size_t>(gx);
            const std::uint8_t* pixel = row + static_cast<std::size_t>(x) * 3;
            // BT.601 luma, integer weights: 299 R + 587 G + 114 B over 1000.
            const int luma = 114 * pixel[0] + 587 * pixel[1] + 299 * pixel[2];
            grid[cell] += static_cast<float>(luma) / 1000.0f / 255.0f;
            ++counts[cell];
        }
    }

    for (std::size_t index = 0; index < cells; ++index) {
        if (counts[index] > 0) {
            grid[index] /= static_cast<float>(counts[index]);
        }
    }
    return grid;
}

double grid_difference(const std::vector<float>& previous, const std::vector<float>& current) {
    if (previous.empty() || previous.size() != current.size()) {
        return 1.0;
    }
    double total = 0.0;
    for (std::size_t index = 0; index < current.size(); ++index) {
        total += std::abs(previous[index] - current[index]);
    }
    return std::min(1.0, total / static_cast<double>(current.size()));
}

}  // namespace capture_detail

ChangeDetector::ChangeDetector(DiffOptions options) : options_(options) {
    if (options_.grid_width <= 0) {
        options_.grid_width = 64;
    }
    if (options_.grid_height <= 0) {
        options_.grid_height = 36;
    }
}

bool ChangeDetector::changed(const ImageView& frame) {
    const std::vector<float> current =
        capture_detail::gray_grid(frame, options_.grid_width, options_.grid_height);
    if (current.empty()) {
        // An unreadable frame is never a reason to skip OCR: skipping it would
        // hide the failure behind a stale subtitle.
        last_difference_ = 1.0;
        return true;
    }
    if (previous_.size() != current.size()) {
        previous_ = current;
        last_difference_ = 1.0;
        return true;
    }
    last_difference_ = capture_detail::grid_difference(previous_, current);
    previous_ = current;
    return !enabled() || last_difference_ > options_.threshold;
}

void ChangeDetector::reset() {
    previous_.clear();
    last_difference_ = 1.0;
}

}  // namespace watashi
