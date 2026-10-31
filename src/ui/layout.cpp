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
    int font_px,
    bool bold,
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
        if (measure(candidate, font_px, bold).width <= max_width || line.empty()) {
            // An empty line always takes the first word, which is then split
            // below if the word alone is wider than the line.
            line = candidate;
        } else {
            out.push_back(line);
            line = word;
        }

        // A single unbreakable token wider than the line is split by
        // characters so the plate keeps its promised width.
        if (!line.empty() && measure(line, font_px, bold).width > max_width) {
            std::string head;
            for (char character : line) {
                const std::string next = head + character;
                if (!head.empty() &&
                    measure(next, font_px, bold).width > max_width) {
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
    const int product = logical * dpi;
    return (product + 48) / 96;  // round half up for positive values
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
    int font_px,
    bool bold,
    const TextMeasure& measure) {
    std::vector<std::string> lines;
    if (max_width < 1) {
        max_width = 1;
    }
    for (const auto& paragraph : split_paragraphs(text)) {
        append_wrapped(paragraph, max_width, font_px, bold, measure, lines);
    }
    return lines;
}

Layout compute_layout(
    const LayoutRequest& request,
    const TextMeasure& measure) {
    const Style& style = request.style;
    const int dpi = request.dpi < 96 ? 96 : request.dpi;

    const int padding_x = logical_px(style.padding_x, dpi);
    const int padding_y = logical_px(style.padding_y, dpi);
    const int line_gap = logical_px(style.line_gap, dpi);
    const int block_gap = logical_px(style.block_gap, dpi);
    const int source_px = logical_px(style.source_px, dpi);
    const int target_px = logical_px(style.target_px, dpi);
    const int status_px = logical_px(style.status_px, dpi);
    const int max_content = logical_px(style.max_width, dpi);
    const int min_content =
        std::max(1, logical_px(style.min_width, dpi) - 2 * padding_x);

    std::vector<std::string> source_lines;
    std::vector<std::string> target_lines;
    std::vector<std::string> status_lines;
    if (!request.source.empty()) {
        source_lines =
            wrap_text(request.source, max_content, source_px, false, measure);
    }
    if (!request.target.empty()) {
        target_lines =
            wrap_text(request.target, max_content, target_px, true, measure);
    }
    if (!request.status.empty()) {
        status_lines =
            wrap_text(request.status, max_content, status_px, false, measure);
    }

    Layout layout;
    layout.radius = logical_px(style.radius, dpi);
    if (source_lines.empty() && target_lines.empty() && status_lines.empty()) {
        return layout;
    }

    auto line_extent = [&measure](
                           const std::string& line, int px, bool bold) {
        return measure(line, px, bold);
    };

    int content_width = min_content;
    for (const auto& line : source_lines) {
        content_width =
            std::max(content_width, line_extent(line, source_px, false).width);
    }
    for (const auto& line : target_lines) {
        content_width =
            std::max(content_width, line_extent(line, target_px, true).width);
    }
    for (const auto& line : status_lines) {
        content_width =
            std::max(content_width, line_extent(line, status_px, false).width);
    }
    content_width = std::min(content_width, max_content);

    layout.width = content_width + 2 * padding_x;

    int y = padding_y;
    auto add_block = [&](const std::vector<std::string>& lines,
                         RunKind kind,
                         int px,
                         bool bold) {
        for (std::size_t index = 0; index < lines.size(); ++index) {
            TextExtent extent = line_extent(lines[index], px, bold);
            TextRun run;
            run.text = lines[index];
            run.kind = kind;
            run.px = px;
            run.x = padding_x;
            run.y = y;
            run.width = extent.width;
            run.height = extent.height;
            layout.runs.push_back(std::move(run));
            y += extent.height;
            if (index + 1 < lines.size()) {
                y += line_gap;
            }
        }
    };

    bool first_block = true;
    auto gap = [&]() {
        if (!first_block) {
            y += block_gap;
        }
        first_block = false;
    };

    if (!source_lines.empty()) {
        gap();
        add_block(source_lines, RunKind::kSource, source_px, false);
    }
    if (!target_lines.empty()) {
        gap();
        add_block(target_lines, RunKind::kTarget, target_px, true);
    }
    if (!status_lines.empty()) {
        gap();
        add_block(status_lines, RunKind::kStatus, status_px, false);
    }

    layout.height = y + padding_y;
    layout.source_lines = static_cast<int>(source_lines.size());
    layout.target_lines = static_cast<int>(target_lines.size());
    layout.status_lines = static_cast<int>(status_lines.size());
    layout.empty = false;

    const CaptureRect placed = anchor_bottom_center(
        request.work_area,
        layout.width,
        layout.height,
        logical_px(style.margin_bottom, dpi),
        logical_px(style.margin_side, dpi));
    layout.x = placed.x;
    layout.y = placed.y;
    return layout;
}

}  // namespace watashi::ui
