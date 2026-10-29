// The frame source: where the pixels a session reads come from.
//
// Three implementations share one seam so the rest of the engine never learns
// whether a frame came from a monitor, from one window or from memory:
//
//   ScreenCapture    GDI BitBlt of a rectangle of the desktop, in physical
//                    pixels, PerMonitorV2 aware.
//   WindowCapture    DWM's extended frame bounds of one HWND, re-read every
//                    frame so a moving window is followed.
//   SyntheticCapture deterministic bytes for CI, which has no display.
//
// Geometry is in physical desktop pixels and may be negative: a monitor placed
// left of the primary starts at a negative x. Every rectangle crossing this
// boundary is clipped against the virtual screen instead of silently producing
// a smaller frame.
//
// ``t_start_s`` / ``t_end_s`` are steady-clock stamps kept for measurement.
// They are deliberately not part of any event: the wire contract has no place
// for them and a client could not compare them with its own clock anyway.

#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include "watashi/ocr.hpp"

namespace watashi {

/// A rectangle in physical desktop pixels. ``x``/``y`` is the top-left corner
/// and may be negative; ``width``/``height`` are positive for a usable
/// rectangle.
struct CaptureRect {
    int x = 0;
    int y = 0;
    int width = 0;
    int height = 0;

    [[nodiscard]] int right() const noexcept { return x + width; }
    [[nodiscard]] int bottom() const noexcept { return y + height; }
};

/// A window that closed, an empty region, a failed BitBlt, no display at all.
class CaptureError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

/// Pure geometry, exposed so the parts that need no display can be tested on a
/// machine without one.
namespace capture_detail {

[[nodiscard]] bool rect_empty(const CaptureRect& rect) noexcept;

/// ``right`` and ``bottom`` are exclusive, like a RECT.
[[nodiscard]] CaptureRect make_rect(int left, int top, int right, int bottom);

/// Intersection of ``wanted`` and ``bounds``. Empty when they do not overlap,
/// which is how an out-of-range region is reported rather than captured as a
/// smaller frame at a moved origin.
[[nodiscard]] CaptureRect clip_rect(const CaptureRect& wanted, const CaptureRect& bounds);

/// Logical points at ``scale`` to physical pixels, each edge rounded once.
[[nodiscard]] CaptureRect scale_rect(const CaptureRect& logical, double scale);

/// ``dpi / 96``: 96 -> 1.0, 144 -> 1.5. Values below 1 dpi clamp to 1.0.
[[nodiscard]] double dpi_scale(int dpi) noexcept;

/// The primary monitor's bounds in physical pixels; all zeros when there is no
/// display, which is the signal the screen tests use to exit 3.
[[nodiscard]] CaptureRect primary_monitor_rect();

/// Bounds of every monitor together (the virtual screen).
[[nodiscard]] CaptureRect virtual_screen_rect();

}  // namespace capture_detail

/// One captured frame. ``bgr`` is width*height*3 bytes, B, G, R order, no
/// padding, top-down -- the same layout ``ImageView`` describes.
struct CaptureFrame {
    std::vector<std::uint8_t> bgr;
    int width = 0;
    int height = 0;
    /// Desktop position of the frame's top-left pixel. Non-zero and possibly
    /// negative, which is what lets a client draw the overlay in the right place.
    int origin_x = 0;
    int origin_y = 0;
    /// Monotonic seconds. Measurement only; never published.
    double t_start_s = 0.0;
    double t_end_s = 0.0;

    [[nodiscard]] ImageView view() const noexcept {
        return ImageView{bgr.empty() ? nullptr : bgr.data(), width, height};
    }
};

/// What a source is looking at, for the ``ready`` event.
struct CaptureInfo {
    std::string mode = "synthetic";
    CaptureRect region;
    std::string description;
};

/// The seam. A source owns no session state: ``grab`` is the whole contract.
class CaptureSource {
public:
    virtual ~CaptureSource() = default;

    /// Called once per frame. Throws CaptureError when the frame cannot be
    /// produced; the caller reports it and stops, because pretending to have
    /// pixels is worse than stopping.
    [[nodiscard]] virtual CaptureFrame grab() = 0;

    /// Stable for the life of the source. ``WindowCapture`` describes the
    /// initial bounds; later frames may have moved.
    [[nodiscard]] virtual CaptureInfo info() const = 0;
};

/// A rectangle of the desktop through BitBlt.
class ScreenCapture final : public CaptureSource {
public:
    struct Options {
        /// Empty means the primary monitor.
        CaptureRect region;
        bool has_region = false;
    };

    explicit ScreenCapture(Options options);

    [[nodiscard]] CaptureFrame grab() override;
    [[nodiscard]] CaptureInfo info() const override;

private:
    CaptureRect region_;
};

/// One top-level window through its extended frame bounds, which DWM reports
/// without the invisible resize border ``GetWindowRect`` includes.
class WindowCapture final : public CaptureSource {
public:
    struct Options {
        std::uintptr_t hwnd = 0;
    };

    explicit WindowCapture(Options options);

    /// Throws CaptureError when the window is gone or minimised, rather than
    /// returning black pixels that look like a real frame.
    [[nodiscard]] CaptureFrame grab() override;
    [[nodiscard]] CaptureInfo info() const override;

private:
    std::uintptr_t hwnd_;
    std::string title_;
    CaptureRect initial_;
};

/// Deterministic frames from memory. ``animate`` moves a white block across a
/// dark background so the change detector can be exercised without a display;
/// otherwise every frame is identical, which is what the CI default wants.
class SyntheticCapture final : public CaptureSource {
public:
    struct Options {
        int width = 1200;
        int height = 200;
        bool animate = false;
    };

    explicit SyntheticCapture(Options options = {});

    [[nodiscard]] CaptureFrame grab() override;
    [[nodiscard]] CaptureInfo info() const override;

private:
    Options options_;
    int tick_ = 0;
};

/// Turn this process PerMonitorV2 DPI aware, falling back to system-DPI
/// awareness on older Windows. Call once, before any geometry is read: after
/// the first window is created Windows locks the awareness in.
/// Returns true when at least one path succeeded.
bool enable_per_monitor_dpi();

/// Window whose title contains ``needle`` (case-insensitive). Returns 0 when
/// none matches. Prefers the shortest match, so "notepad" finds the editor
/// rather than a browser tab that happens to mention it.
[[nodiscard]] std::uintptr_t find_window_by_title(std::string_view needle);

/// UTF-8 title of ``hwnd``, empty when it is gone or untitled.
[[nodiscard]] std::string window_title(std::uintptr_t hwnd);

/// Downsampled grey difference used to skip OCR on a still screen.
struct DiffOptions {
    /// Mean absolute difference in [0, 1] on the grey grid. <= 0 disables the
    /// check, so every frame is treated as changed.
    double threshold = 0.0;
    int grid_width = 64;
    int grid_height = 36;
};

namespace capture_detail {

/// Box-averaged grey (BT.601 luma) samples in [0, 1].
[[nodiscard]] std::vector<float> gray_grid(const ImageView& frame, int grid_width, int grid_height);

/// Mean absolute difference of two equal grids, clamped to [0, 1].
[[nodiscard]] double grid_difference(const std::vector<float>& previous,
                                     const std::vector<float>& current);

}  // namespace capture_detail

/// Remembers the last frame's grey grid and answers whether the new one differs
/// by more than ``threshold``. The first frame always counts as changed: there
/// is nothing to compare it with.
class ChangeDetector {
public:
    explicit ChangeDetector(DiffOptions options = {});

    /// True when OCR should run for this frame.
    [[nodiscard]] bool changed(const ImageView& frame);

    [[nodiscard]] double last_difference() const noexcept { return last_difference_; }
    [[nodiscard]] bool enabled() const noexcept { return options_.threshold > 0.0; }
    void reset();

private:
    DiffOptions options_;
    std::vector<float> previous_;
    double last_difference_ = 1.0;
};

}  // namespace watashi
