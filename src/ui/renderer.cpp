#include "watashi/ui/renderer.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <string>
#include <vector>

namespace watashi::ui {
namespace {

constexpr wchar_t kFontFace[] = L"Microsoft YaHei UI";

std::wstring to_wide(const std::string& utf8) {
    if (utf8.empty()) {
        return std::wstring();
    }
    const int length = MultiByteToWideChar(
        CP_UTF8, 0, utf8.data(), static_cast<int>(utf8.size()), nullptr, 0);
    if (length <= 0) {
        return std::wstring();
    }
    std::wstring wide(static_cast<std::size_t>(length), L'\0');
    MultiByteToWideChar(
        CP_UTF8, 0, utf8.data(), static_cast<int>(utf8.size()), wide.data(), length);
    return wide;
}

struct Dib {
    HBITMAP bitmap = nullptr;
    HDC dc = nullptr;
    void* bits = nullptr;
    int width = 0;
    int height = 0;

    ~Dib() { reset(); }

    void reset() {
        if (dc != nullptr) {
            DeleteDC(dc);
            dc = nullptr;
        }
        if (bitmap != nullptr) {
            DeleteObject(bitmap);
            bitmap = nullptr;
        }
        bits = nullptr;
        width = 0;
        height = 0;
    }

    bool create(int w, int h) {
        reset();
        if (w <= 0 || h <= 0) {
            return false;
        }
        BITMAPINFO info{};
        info.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        info.bmiHeader.biWidth = w;
        info.bmiHeader.biHeight = -h;  // top-down
        info.bmiHeader.biPlanes = 1;
        info.bmiHeader.biBitCount = 32;
        info.bmiHeader.biCompression = BI_RGB;

        HDC screen = GetDC(nullptr);
        bitmap = CreateDIBSection(screen, &info, DIB_RGB_COLORS, &bits, nullptr, 0);
        ReleaseDC(nullptr, screen);
        if (bitmap == nullptr || bits == nullptr) {
            reset();
            return false;
        }
        dc = CreateCompatibleDC(nullptr);
        if (dc == nullptr) {
            reset();
            return false;
        }
        SelectObject(dc, bitmap);
        std::memset(bits, 0, static_cast<std::size_t>(w) * h * 4);
        width = w;
        height = h;
        return true;
    }
};

std::uint8_t clamp_byte(int value) {
    return static_cast<std::uint8_t>(std::max(0, std::min(255, value)));
}

/// Coverage of a rounded rectangle at pixel ``px, py``, 0..255 with one pixel
/// of anti-aliasing on the corner arc.
std::uint8_t plate_coverage(int px, int py, int width, int height, int radius) {
    if (px < 0 || py < 0 || px >= width || py >= height) {
        return 0;
    }
    if (radius <= 1) {
        return 255;
    }
    // Distance from the nearest corner centre.
    const int cx = std::max(radius, std::min(px, width - 1 - radius));
    const int cy = std::max(radius, std::min(py, height - 1 - radius));
    const double dx = static_cast<double>(px - cx);
    const double dy = static_cast<double>(py - cy);
    const double distance = std::sqrt(dx * dx + dy * dy);
    const double alpha = std::min(1.0, std::max(0.0, radius + 0.5 - distance));
    return static_cast<std::uint8_t>(alpha * 255.0);
}

void blend_text(
    std::uint8_t* pixel,
    std::uint8_t coverage,
    std::uint8_t r,
    std::uint8_t g,
    std::uint8_t b) {
    const int cov = coverage;
    const int keep = 255 - cov;
    pixel[0] = clamp_byte((b * cov + pixel[0] * keep) / 255);
    pixel[1] = clamp_byte((g * cov + pixel[1] * keep) / 255);
    pixel[2] = clamp_byte((r * cov + pixel[2] * keep) / 255);
    pixel[3] = clamp_byte(cov + pixel[3] * keep / 255);
}

std::uint8_t run_color_or_status(const Theme& theme, RunKind kind, int channel) {
    switch (kind) {
        case RunKind::kSource:
            return channel == 0 ? theme.source_r : channel == 1 ? theme.source_g
                                                                : theme.source_b;
        case RunKind::kTarget:
            return channel == 0 ? theme.target_r : channel == 1 ? theme.target_g
                                                                : theme.target_b;
        case RunKind::kStatus:
            break;
    }
    return channel == 0 ? theme.status_r : channel == 1 ? theme.status_g
                                                        : theme.status_b;
}

}  // namespace

GdiText::GdiText() {
    dc_ = CreateCompatibleDC(nullptr);
}

GdiText::~GdiText() {
    for (auto& entry : fonts_) {
        if (entry.second != nullptr) {
            DeleteObject(entry.second);
        }
    }
    fonts_.clear();
    if (dc_ != nullptr) {
        DeleteDC(dc_);
    }
}

HFONT GdiText::select_font(HDC dc, int px, bool bold) {
    const auto key = std::make_pair(px, bold);
    const auto found = fonts_.find(key);
    HFONT font = nullptr;
    if (found != fonts_.end()) {
        font = found->second;
    } else {
        font = CreateFontW(
            -px, 0, 0, 0,
            bold ? FW_SEMIBOLD : FW_NORMAL,
            FALSE, FALSE, FALSE,
            DEFAULT_CHARSET,
            OUT_TT_PRECIS,
            CLIP_DEFAULT_PRECIS,
            ANTIALIASED_QUALITY,
            DEFAULT_PITCH | FF_DONTCARE,
            kFontFace);
        fonts_.emplace(key, font);
    }
    if (dc != nullptr && font != nullptr) {
        SelectObject(dc, font);
    }
    return font;
}

TextExtent GdiText::measure(const std::string& utf8, int px) {
    if (dc_ == nullptr) {
        return TextExtent{};
    }
    const std::wstring wide = to_wide(utf8);
    HFONT font = select_font(dc_, px, false);
    TextExtent extent;
    if (font == nullptr) {
        extent.height = px;
        return extent;
    }
    if (!wide.empty()) {
        SIZE size{};
        GetTextExtentPoint32W(dc_, wide.c_str(), static_cast<int>(wide.size()), &size);
        extent.width = static_cast<int>(size.cx);
    }
    TEXTMETRICW metrics{};
    GetTextMetricsW(dc_, &metrics);
    extent.height = static_cast<int>(metrics.tmHeight);
    return extent;
}

TextMeasure GdiText::as_measure() {
    return [this](const std::string& text, int px) { return measure(text, px); };
}

OverlayImage compose_overlay(
    const Layout& layout,
    GdiText& text,
    const Theme& theme) {
    OverlayImage image;
    if (layout.empty || layout.width <= 0 || layout.height <= 0) {
        return image;
    }

    Dib plate;
    if (!plate.create(layout.width, layout.height)) {
        return image;
    }
    auto* pixels = static_cast<std::uint8_t*>(plate.bits);
    const int width = layout.width;
    const int height = layout.height;
    const int radius = std::max(0, layout.radius);

    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            const std::uint8_t coverage = plate_coverage(x, y, width, height, radius);
            if (coverage == 0) {
                continue;
            }
            const int alpha = static_cast<int>(coverage) * theme.plate_alpha / 255;
            std::uint8_t* pixel = pixels + (static_cast<std::size_t>(y) * width + x) * 4;
            pixel[0] = clamp_byte(theme.plate_b * alpha / 255);
            pixel[1] = clamp_byte(theme.plate_g * alpha / 255);
            pixel[2] = clamp_byte(theme.plate_r * alpha / 255);
            pixel[3] = static_cast<std::uint8_t>(alpha);
        }
    }

    // Text goes into a separate black-and-white surface; its coverage is then
    // multiplied into the plate buffer. Drawing straight into the plate would
    // produce opaque glyphs and no soft edges.
    Dib mask;
    if (!mask.create(layout.width, layout.height)) {
        return image;
    }
    SetBkMode(mask.dc, TRANSPARENT);
    SetTextColor(mask.dc, RGB(255, 255, 255));
    SetTextAlign(mask.dc, TA_LEFT | TA_TOP);
    for (const auto& run : layout.runs) {
        if (run.text.empty()) {
            continue;
        }
        const std::wstring wide = to_wide(run.text);
        if (wide.empty()) {
            continue;
        }
        text.select_font(mask.dc, run.px, run.kind == RunKind::kTarget);
        TextOutW(mask.dc, run.x, run.y, wide.c_str(), static_cast<int>(wide.size()));
    }

    const auto* coverage_bits = static_cast<const std::uint8_t*>(mask.bits);
    for (const auto& run : layout.runs) {
        const int x0 = std::max(0, run.x);
        const int y0 = std::max(0, run.y);
        const int x1 = std::min(width, run.x + std::max(1, run.width));
        const int y1 = std::min(height, run.y + std::max(1, run.height));
        const std::uint8_t color_r = run_color_or_status(theme, run.kind, 0);
        const std::uint8_t color_g = run_color_or_status(theme, run.kind, 1);
        const std::uint8_t color_b = run_color_or_status(theme, run.kind, 2);
        for (int y = y0; y < y1; ++y) {
            for (int x = x0; x < x1; ++x) {
                const std::uint8_t* coverage =
                    coverage_bits + (static_cast<std::size_t>(y) * width + x) * 4;
                // GDI antialiases with grey; ClearType would tint the channels,
                // so the maximum channel is the coverage for both cases.
                const std::uint8_t alpha =
                    std::max(coverage[0], std::max(coverage[1], coverage[2]));
                if (alpha == 0) {
                    continue;
                }
                std::uint8_t* pixel =
                    pixels + (static_cast<std::size_t>(y) * width + x) * 4;
                blend_text(pixel, alpha, color_r, color_g, color_b);
            }
        }
    }

    image.width = layout.width;
    image.height = layout.height;
    image.bgra.assign(
        pixels,
        pixels + static_cast<std::size_t>(width) * height * 4);
    return image;
}

bool write_bmp(const std::string& path, const OverlayImage& image) {
    if (image.empty()) {
        return false;
    }
    std::ofstream file(std::filesystem::path(path), std::ios::binary | std::ios::trunc);
    if (!file) {
        std::fprintf(stderr, "write_bmp: cannot open %s\n", path.c_str());
        return false;
    }

    const std::uint32_t pixel_bytes =
        static_cast<std::uint32_t>(image.width) * static_cast<std::uint32_t>(image.height) * 4;
    const std::uint32_t file_size = 14 + 40 + pixel_bytes;

    std::uint8_t file_header[14]{};
    file_header[0] = 'B';
    file_header[1] = 'M';
    std::memcpy(file_header + 2, &file_size, 4);
    const std::uint32_t offset = 54;
    std::memcpy(file_header + 10, &offset, 4);

    std::uint8_t info[40]{};
    const std::uint32_t header_size = 40;
    const std::int32_t width = image.width;
    const std::int32_t height = -image.height;  // top-down
    const std::uint16_t planes = 1;
    const std::uint16_t bits = 32;
    std::memcpy(info + 0, &header_size, 4);
    std::memcpy(info + 4, &width, 4);
    std::memcpy(info + 8, &height, 4);
    std::memcpy(info + 12, &planes, 2);
    std::memcpy(info + 14, &bits, 2);
    std::memcpy(info + 20, &pixel_bytes, 4);

    file.write(reinterpret_cast<const char*>(file_header), sizeof(file_header));
    file.write(reinterpret_cast<const char*>(info), sizeof(info));

    // Flatten premultiplied alpha onto a dark neutral background so the BMP is
    // legible on its own. Alpha is filled with 255.
    const std::uint8_t bg_b = 32;
    const std::uint8_t bg_g = 32;
    const std::uint8_t bg_r = 36;
    std::vector<std::uint8_t> row(static_cast<std::size_t>(image.width) * 4);
    for (int y = 0; y < image.height; ++y) {
        for (int x = 0; x < image.width; ++x) {
            const std::uint8_t* pixel =
                image.bgra.data() + (static_cast<std::size_t>(y) * image.width + x) * 4;
            const int keep = 255 - pixel[3];
            row[static_cast<std::size_t>(x) * 4 + 0] =
                clamp_byte(pixel[0] + bg_b * keep / 255);
            row[static_cast<std::size_t>(x) * 4 + 1] =
                clamp_byte(pixel[1] + bg_g * keep / 255);
            row[static_cast<std::size_t>(x) * 4 + 2] =
                clamp_byte(pixel[2] + bg_r * keep / 255);
            row[static_cast<std::size_t>(x) * 4 + 3] = 255;
        }
        file.write(reinterpret_cast<const char*>(row.data()),
                   static_cast<std::streamsize>(row.size()));
    }
    return static_cast<bool>(file);
}

}  // namespace watashi::ui
