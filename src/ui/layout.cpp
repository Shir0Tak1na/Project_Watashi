#include "watashi/ui/layout.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <string>
#include <utility>
#include <vector>

namespace watashi::ui {
namespace {

std::vector<std::string> split_paragraphs(const std::string& text) {
    std::vector<std::string> paragraphs;
    std::size_t start = 0;
    while (start <= text.size()) {
        const auto newline = text.find('\n', start);
        std::string paragraph =
            text.substr(start, newline == std::string::npos ? std::string::npos
                                                            : newline - start);
        if (!paragraph.empty() && paragraph.back() == '\r') {
            paragraph.pop_back();
        }
        paragraphs.push_back(std::move(paragraph));
        if (newline == std::string::npos) {
            break;
        }
        start = newline + 1;
    }
    return paragraphs;
}

void append_wrapped(
    const std::string& paragraph,
    int max_width,
    const MeasureStyle& style,
    const TextMeasure& measure,
    std::vector<std::string>& out) {
    if (paragraph.empty()) {
        out.emplace_back();
        return;
    }

    std::string line;
    for (std::size_t index = 0; index < paragraph.size();) {
        const auto space = paragraph.find(' ', index);
        const std::size_t end =
            space == std::string::npos ? paragraph.size() : space;
        const std::string word = paragraph.substr(index, end - index);
        index = space == std::string::npos ? paragraph.size() : space + 1;

        const std::string candidate = line.empty() ? word : line + " " + word;
        if (measure(candidate, style).width <= max_width || line.empty()) {
            line = candidate;
        } else {
            out.push_back(line);
            line = word;
        }

        // A single unbreakable token wider than the line is split by
        // characters so the plate keeps its promised width.
        if (!line.empty() && measure(line, style).width > max_width) {
            std::string head;
            for (char character : line) {
                const std::string next = head + character;
                if (!head.empty() && measure(next, style).width > max_width) {
                    out.push_back(head);
                    head = std::string(1, character);
                } else {
                    head = next;
                }
            }
            line = head;
        }
    }
    if (!line.empty() || out.empty()) {
        out.push_back(line);
    }
}

RunKind kind_for(const std::string& role) {
    if (role == "source") {
        return RunKind::kSource;
    }
    if (role == "trace") {
        return RunKind::kTrace;
    }
    if (role == "previous") {
        return RunKind::kPrevious;
    }
    return RunKind::kTarget;
}

std::string text_for(const std::string& role, const PresentationText& text) {
    if (role == "source") {
        return text.source;
    }
    if (role == "target") {
        return text.target;
    }
    if (role == "trace") {
        return text.trace;
    }
    if (role == "previous") {
        return text.previous;
    }
    return std::string();
}

bool has_text(const std::string& value) {
    return value.find_first_not_of(" \t\r\n") != std::string::npos;
}

struct StagedLine {
    std::string text;
    TextExtent extent;
};

struct StagedBlock {
    RunKind kind = RunKind::kTarget;
    RunStyle style;
    std::vector<StagedLine> lines;
};

}  // namespace

double dpi_scale(int dpi) noexcept {
    if (dpi < 96) {
        return 1.0;
    }
    return static_cast<double>(dpi) / 96.0;
}

int logical_px(int logical, int dpi) noexcept {
    if (dpi < 96) {
        dpi = 96;
    }
    if (logical >= 0) {
        return (logical * dpi + 48) / 96;
    }
    return -((-logical * dpi + 48) / 96);
}

Point anchor_position(
    Anchor anchor,
    const CaptureRect& work_area,
    int width,
    int height,
    int offset_x,
    int offset_y) noexcept {
    const bool left = anchor == Anchor::kTopLeft ||
                      anchor == Anchor::kMiddleLeft ||
                      anchor == Anchor::kBottomLeft;
    const bool right = anchor == Anchor::kTopRight ||
                       anchor == Anchor::kMiddleRight ||
                       anchor == Anchor::kBottomRight;
    const bool top = anchor == Anchor::kTopLeft ||
                     anchor == Anchor::kTopCenter ||
                     anchor == Anchor::kTopRight;
    const bool bottom = anchor == Anchor::kBottomLeft ||
                        anchor == Anchor::kBottomCenter ||
                        anchor == Anchor::kBottomRight;

    int x = work_area.x;
    if (right) {
        x = work_area.right() - width;
    } else if (!left) {
        x = work_area.x + (work_area.width - width) / 2;
    }
    int y = work_area.y;
    if (bottom) {
        y = work_area.bottom() - height;
    } else if (!top) {
        y = work_area.y + (work_area.height - height) / 2;
    }

    x += offset_x;
    y += offset_y;

    if (width >= work_area.width) {
        x = work_area.x;
    } else {
        x = std::max(work_area.x, std::min(x, work_area.right() - width));
    }
    if (height >= work_area.height) {
        y = work_area.y;
    } else {
        y = std::max(work_area.y, std::min(y, work_area.bottom() - height));
    }
    return Point{x, y};
}

CaptureRect anchor_bottom_center(
    const CaptureRect& work_area,
    int width,
    int height,
    int margin_bottom,
    int margin_side) noexcept {
    CaptureRect placed;
    placed.width = width;
    placed.height = height;

    if (width >= work_area.width) {
        placed.x = work_area.x;
    } else {
        placed.x = work_area.x + (work_area.width - width) / 2;
        const int left_limit = work_area.x + margin_side;
        const int right_limit = work_area.right() - margin_side - width;
        if (right_limit >= left_limit) {
            placed.x = std::max(left_limit, std::min(placed.x, right_limit));
        }
    }

    placed.y = work_area.bottom() - margin_bottom - height;
    if (placed.y < work_area.y) {
        placed.y = work_area.y;
    }
    return placed;
}

std::vector<std::string> wrap_text(
    const std::string& text,
    int max_width,
    const MeasureStyle& style,
    const TextMeasure& measure) {
    std::vector<std::string> lines;
    if (max_width < 1) {
        max_width = 1;
    }
    for (const auto& paragraph : split_paragraphs(text)) {
        append_wrapped(paragraph, max_width, style, measure, lines);
    }
    return lines;
}

MeasureStyle measure_style(const FontSpec& font, int dpi) {
    MeasureStyle style;
    style.px = logical_px(font.size, dpi);
    style.bold = font.bold;
    style.italic = font.italic;
    style.family = font.family;
    return style;
}

Layout compute_layout(
    const LayoutRequest& request,
    const TextMeasure& measure) {
    const PresentationSpec& spec = request.spec;
    const int dpi = request.dpi < 96 ? 96 : request.dpi;

    Layout layout;
    layout.background.plate = spec.background.kind == BackgroundKind::kPlate;
    layout.background.color = spec.background.color;
    layout.background.opacity = spec.background.opacity;
    layout.background.radius = logical_px(spec.background.radius, dpi);

    if (spec.layout.mode == LayoutMode::kHidden) {
        layout.hidden = true;
        return layout;
    }

    const CaptureRect& work_area = request.work_area;
    const int padding_x = logical_px(spec.background.padding_x, dpi);
    const int padding_y = logical_px(spec.background.padding_y, dpi);
    const int line_gap = logical_px(spec.layout.line_gap, dpi);
    const int max_width = std::max(
        1,
        static_cast<int>(std::lround(
            static_cast<double>(work_area.width) * spec.layout.max_width_ratio)));
    const int content_limit = std::max(1, max_width - 2 * padding_x);

    const double signal = std::min(request.text.confidence, request.text.coverage);
    const bool dim =
        spec.confidence.dim_below > 0.0 && signal < spec.confidence.dim_below;

    std::vector<StagedBlock> blocks;
    int content_width = 0;

    for (const auto& element : spec.ordered_elements()) {
        const std::string value = text_for(element.role, request.text);
        if (!has_text(value)) {
            continue;
        }
        StagedBlock block;
        block.kind = kind_for(element.role);
        block.style.metrics = measure_style(element.font, dpi);
        block.style.color = element.color;
        block.style.opacity = element.opacity;
        if (dim && element.role == "target") {
            block.style.opacity =
                std::min(block.style.opacity, spec.confidence.dim_opacity);
        }
        if (element.has_outline) {
            block.style.outline_px = logical_px(element.outline.width, dpi);
            block.style.outline_color = element.outline.color;
        }

        std::vector<std::string> wrapped =
            wrap_text(value, content_limit, block.style.metrics, measure);
        if (element.max_lines > 0 &&
            wrapped.size() > static_cast<std::size_t>(element.max_lines)) {
            wrapped.resize(static_cast<std::size_t>(element.max_lines));
        }
        for (const auto& line : wrapped) {
            StagedLine staged;
            staged.text = line;
            staged.extent = measure(line, block.style.metrics);
            content_width = std::max(content_width, staged.extent.width);
            block.lines.push_back(std::move(staged));
        }
        if (!block.lines.empty()) {
            blocks.push_back(std::move(block));
        }
    }

    // Status is chrome rather than spec content: it stays on a fixed, readable
    // style so a design cannot accidentally hide a stream error.
    StagedBlock status;
    if (has_text(request.text.status)) {
        status.kind = RunKind::kStatus;
        status.style.metrics = MeasureStyle{logical_px(12, dpi), false, false, ""};
        status.style.color = Rgb{236, 196, 112};
        status.style.opacity = 1.0;
        for (const auto& line :
             wrap_text(request.text.status, content_limit, status.style.metrics, measure)) {
            StagedLine staged;
            staged.text = line;
            staged.extent = measure(line, status.style.metrics);
            content_width = std::max(content_width, staged.extent.width);
            status.lines.push_back(std::move(staged));
        }
        if (!status.lines.empty()) {
            blocks.push_back(std::move(status));
        }
    }

    if (blocks.empty()) {
        return layout;
    }

    content_width = std::min(content_width, content_limit);
    layout.width = content_width + 2 * padding_x;

    int y = padding_y;
    bool first_block = true;
    for (const auto& block : blocks) {
        if (!first_block) {
            y += line_gap;
        }
        first_block = false;
        for (std::size_t index = 0; index < block.lines.size(); ++index) {
            const auto& line = block.lines[index];
            int line_x = padding_x;
            if (spec.layout.align == Align::kCenter) {
                line_x += std::max(0, (content_width - line.extent.width) / 2);
            } else if (spec.layout.align == Align::kRight) {
                line_x += std::max(0, content_width - line.extent.width);
            }
            TextRun run;
            run.text = line.text;
            run.kind = block.kind;
            run.style = block.style;
            run.x = line_x;
            run.y = y;
            run.width = line.extent.width;
            run.height = line.extent.height;
            layout.runs.push_back(std::move(run));
            y += line.extent.height;
            if (index + 1 < block.lines.size()) {
                y += line_gap;
            }
        }
    }
    layout.height = y + padding_y;
    layout.empty = false;

    for (const auto& run : layout.runs) {
        switch (run.kind) {
            case RunKind::kSource:
                ++layout.source_lines;
                break;
            case RunKind::kTarget:
                ++layout.target_lines;
                break;
            case RunKind::kStatus:
                ++layout.status_lines;
                break;
            default:
                break;
        }
    }

    const Point placed = anchor_position(
        spec.layout.anchor,
        work_area,
        layout.width,
        layout.height,
        logical_px(spec.layout.offset_x, dpi),
        logical_px(spec.layout.offset_y, dpi));
    layout.x = placed.x;
    layout.y = placed.y;
    return layout;
}

}  // namespace watashi::ui
