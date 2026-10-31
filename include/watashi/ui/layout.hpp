// Pure overlay geometry: DPI conversion, wrapping, anchoring and the
// presentation spec applied to pixels.
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
#include "watashi/ui/presentation.hpp"

namespace watashi::ui {

struct TextExtent {
    int width = 0;
    int height = 0;
};

/// Everything that affects glyph width: physical size, weight and face. The
/// renderer must paint with exactly these values or a wrapped line is clipped.
struct MeasureStyle {
    int px = 12;
    bool bold = false;
    bool italic = false;
    std::string family;
};

/// UTF-8 text -> physical extent, measured with ``style``.
using TextMeasure =
    std::function<TextExtent(const std::string& utf8, const MeasureStyle& style)>;

enum class RunKind { kSource, kTarget, kTrace, kPrevious, kStatus };

struct RunStyle {
    MeasureStyle metrics;
    Rgb color{255, 255, 255};
    /// Effective opacity: element opacity, dimmed when confidence is low.
    double opacity = 1.0;
    int outline_px = 0;
    Rgb outline_color{0, 0, 0};
};

struct TextRun {
    std::string text;
    RunKind kind = RunKind::kTarget;
    RunStyle style;
    /// Relative to the overlay's top-left, physical pixels.
    int x = 0;
    int y = 0;
    int width = 0;
    int height = 0;
};

struct BackgroundPaint {
    bool plate = true;
    Rgb color{0, 0, 0};
    double opacity = 0.72;
    int radius = 0;
};

struct Layout {
    /// Physical pixels. Zero when there is nothing to paint.
    int width = 0;
    int height = 0;
    /// Physical screen position (anchor already applied).
    int x = 0;
    int y = 0;
    bool empty = true;
    /// ``mode: hidden``: process the stream but draw nothing.
    bool hidden = false;
    std::vector<TextRun> runs;
    BackgroundPaint background;
    int source_lines = 0;
    int target_lines = 0;
    int status_lines = 0;
};

struct PresentationText {
    std::string source;
    std::string target;
    std::string trace;
    std::string previous;
    std::string status;
    /// 0..1. The spec's "confidence" dimming uses whichever of the two the
    /// pipeline reports lower, so a partial corpus translation dims like the
    /// prototype does.
    double confidence = 1.0;
    double coverage = 1.0;
};

struct LayoutRequest {
    PresentationText text;
    /// The monitor's work area in physical pixels.
    CaptureRect work_area;
    /// 96, 120 or 144 on this project; any value >= 96 is accepted.
    int dpi = 96;
    PresentationSpec spec;
};

/// ``dpi / 96``, clamped to at least 1.0.
[[nodiscard]] double dpi_scale(int dpi) noexcept;

/// Logical points to physical pixels, rounding halves up. Negative input keeps
/// the sign; all callers pass non-negative style values.
[[nodiscard]] int logical_px(int logical, int dpi) noexcept;

struct Point {
    int x = 0;
    int y = 0;
};

/// Top-left for a block of ``width``/``height`` at one of the nine anchors,
/// plus a logical offset. The result is clamped inside the work area.
[[nodiscard]] Point anchor_position(
    Anchor anchor,
    const CaptureRect& work_area,
    int width,
    int height,
    int offset_x,
    int offset_y) noexcept;

/// Kept from W3.0: bottom-centre with independent bottom/side margins.
[[nodiscard]] CaptureRect anchor_bottom_center(
    const CaptureRect& work_area,
    int width,
    int height,
    int margin_bottom,
    int margin_side) noexcept;

/// Greedy word wrap at ``max_width`` physical pixels, honouring explicit
/// newlines. A word longer than the line is hard-split at the width instead of
/// overflowing the plate.
[[nodiscard]] std::vector<std::string> wrap_text(
    const std::string& text,
    int max_width,
    const MeasureStyle& style,
    const TextMeasure& measure);

/// Lay out the spec's elements plus the status line and apply the anchor.
/// ``mode: hidden`` yields an empty layout with ``hidden`` set.
[[nodiscard]] Layout compute_layout(
    const LayoutRequest& request,
    const TextMeasure& measure);

/// The physical style an element's logical font becomes at ``dpi``.
[[nodiscard]] MeasureStyle measure_style(const FontSpec& font, int dpi);

}  // namespace watashi::ui
