// Pure overlay geometry: DPI conversion, wrapping and the bottom-centre anchor.
//
// Nothing here touches a window or a device context. Text is measured through
// an injected callback, so the same code is exercised by the unit tests with a
// deterministic fake metric and by the executable with GDI. That is the only
// way "the anchor is correct at 125%" can be asserted on a machine whose
// display is not at 125%.

#pragma once

#include <functional>
#include <string>
#include <vector>

#include "watashi/capture.hpp"

namespace watashi::ui {

struct TextExtent {
    int width = 0;
    int height = 0;
};

/// UTF-8 text at ``px`` physical pixels -> physical extent. Injected so layout
/// stays free of GDI. ``bold`` must be the same weight the renderer will paint
/// with: measuring regular and painting semibold yields a box narrower than the
/// glyphs, which clips the right edge of a target line.
using TextMeasure =
    std::function<TextExtent(const std::string& utf8, int px, bool bold)>;

struct Style {
    /// Logical (96 dpi) metrics. Every value is scaled once by the target dpi.
    int max_width = 700;
    int min_width = 240;
    int padding_x = 20;
    int padding_y = 14;
    int source_px = 15;
    int target_px = 24;
    int status_px = 12;
    int line_gap = 5;
    int block_gap = 10;
    int margin_bottom = 110;
    int margin_side = 24;
    int radius = 14;
};

enum class RunKind { kSource, kTarget, kStatus };

struct TextRun {
    std::string text;
    RunKind kind = RunKind::kTarget;
    int px = 0;
    /// Relative to the overlay's top-left, physical pixels.
    int x = 0;
    int y = 0;
    int width = 0;
    int height = 0;
};

struct Layout {
    /// Physical pixels. Zero when there is nothing to paint.
    int width = 0;
    int height = 0;
    /// Physical screen position (anchor already applied).
    int x = 0;
    int y = 0;
    int radius = 0;
    bool empty = true;
    std::vector<TextRun> runs;
    int source_lines = 0;
    int target_lines = 0;
    int status_lines = 0;
};

struct LayoutRequest {
    std::string source;
    std::string target;
    std::string status;
    /// The monitor's work area in physical pixels.
    CaptureRect work_area;
    /// 96, 120 or 144 on this project; any value >= 96 is accepted.
    int dpi = 96;
    Style style;
};

/// ``dpi / 96``, clamped to at least 1.0.
[[nodiscard]] double dpi_scale(int dpi) noexcept;

/// Logical points to physical pixels, rounding halves up. Negative input keeps
/// the sign; all callers pass non-negative style values.
[[nodiscard]] int logical_px(int logical, int dpi) noexcept;

/// Bottom-centre position inside ``work_area``. The result is clamped so the
/// window never leaves the work area horizontally or above its top edge; the
/// bottom margin is maintained unless the content is taller than the area.
[[nodiscard]] CaptureRect anchor_bottom_center(
    const CaptureRect& work_area,
    int width,
    int height,
    int margin_bottom,
    int margin_side) noexcept;

/// Greedy word wrap at ``max_width`` physical pixels, honouring explicit
/// newlines. A word longer than the line is hard-split at the width instead of
/// overflowing the plate. ``bold`` selects the weight used for measurement.
[[nodiscard]] std::vector<std::string> wrap_text(
    const std::string& text,
    int max_width,
    int font_px,
    bool bold,
    const TextMeasure& measure);

/// Lay out source, target and status; apply the anchor. Empty text with no
/// status yields ``Layout::empty``.
[[nodiscard]] Layout compute_layout(
    const LayoutRequest& request,
    const TextMeasure& measure);

}  // namespace watashi::ui
