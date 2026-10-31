#include "watashi/ui/renderer.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace watashi::ui {
namespace {

constexpr wchar_t kDefaultFace[] = L"Microsoft YaHei UI";

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
    const int cx = std::max(radius, std::min(px, width - 1 - radius));
    const int cy = std::max(radius, std::min(py, height - 1 - radius));
    const double dx = static_cast<double>(px - cx);
    const double dy = static_cast<double>(py - cy);
    const double distance = std::sqrt(dx * dx + dy * dy);
    const double alpha = std::min(1.0, std::max(0.0, radius + 0.5 - distance));
    return static_cast<std::uint8_t>(alpha * 255.0);
}

void blend(std::uint8_t* pixel,
           std::uint8_t coverage,
           std::uint8_t alpha,
           Rgb color) {
    const int cov = static_cast<int>(coverage) * alpha / 255;
    const int keep = 255 - cov;
    pixel[0] = clamp_byte((color.b * cov + pixel[0] * keep) / 255);
    pixel[1] = clamp_byte((color.g * cov + pixel[1] * keep) / 255);
    pixel[2] = clamp_byte((color.r * cov + pixel[2] * keep) / 255);
    pixel[3] = clamp_byte(cov + pixel[3] * keep / 255);
}

std::uint8_t mask_alpha(const Dib& mask, int x, int y) {
    if (x < 0 || y < 0 || x >= mask.width || y >= mask.height) {
        return 0;
    }
    const std::uint8_t* pixel =
        static_cast<const std::uint8_t*>(mask.bits) +
        (static_cast<std::size_t>(y) * mask.width + x) * 4;
    return std::max(pixel[0], std::max(pixel[1], pixel[2]));
}

void clear_rect(Dib& mask, int x0, int y0, int x1, int y1) {
    x0 = std::max(0, x0);
    y0 = std::max(0, y0);
    x1 = std::min(mask.width, x1);
    y1 = std::min(mask.height, y1);
    if (x0 >= x1 || y0 >= y1) {
        return;
    }
    auto* bits = static_cast<std::uint8_t*>(mask.bits);
    for (int y = y0; y < y1; ++y) {
        std::memset(
            bits + (static_cast<std::size_t>(y) * mask.width + x0) * 4,
            0,
            static_cast<std::size_t>(x1 - x0) * 4);
    }
}

}  // namespace

bool GdiText::FontKey::operator<(const FontKey& other) const {
    if (family != other.family) {
        return family < other.family;
    }
    if (px != other.px) {
        return px < other.px;
    }
    if (bold != other.bold) {
        return bold < other.bold;
    }
    return italic < other.italic;
}

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

HFONT GdiText::select_font(HDC dc, const MeasureStyle& style) {
    FontKey key{style.family, style.px, style.bold, style.italic};
    const auto found = fonts_.find(key);
    HFONT font = nullptr;
    if (found != fonts_.end()) {
        font = found->second;
    } else {
        const std::wstring face =
            style.family.empty() ? std::wstring(kDefaultFace) : to_wide(style.family);
        font = CreateFontW(
            -style.px, 0, 0, 0,
            style.bold ? FW_SEMIBOLD : FW_NORMAL,
            style.italic ? TRUE : FALSE,
            FALSE, FALSE,
            DEFAULT_CHARSET,
            OUT_TT_PRECIS,
            CLIP_DEFAULT_PRECIS,
            ANTIALIASED_QUALITY,
            DEFAULT_PITCH | FF_DONTCARE,
            face.c_str());
        fonts_.emplace(key, font);
    }
    if (dc != nullptr && font != nullptr) {
        SelectObject(dc, font);
    }
    return font;
}

TextExtent GdiText::measure(const std::string& utf8, const MeasureStyle& style) {
    if (dc_ == nullptr) {
        return TextExtent{};
    }
    const std::wstring wide = to_wide(utf8);
    HFONT font = select_font(dc_, style);
    TextExtent extent;
    if (font == nullptr) {
        extent.height = style.px;
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
    return [this](const std::string& text, const MeasureStyle& style) {
        return measure(text, style);
    };
}

OverlayImage compose_overlay(const Layout& layout, GdiText& text) {
    OverlayImage image;
    if (layout.empty || layout.hidden || layout.width <= 0 || layout.height <= 0) {
        return image;
    }

    Dib buffer;
    if (!buffer.create(layout.width, layout.height)) {
        return image;
    }
    auto* pixels = static_cast<std::uint8_t*>(buffer.bits);
    const int width = layout.width;
    const int height = layout.height;

    if (layout.background.plate) {
        const int radius = std::max(0, layout.background.radius);
        const int opacity = static_cast<int>(
            std::lround(layout.background.opacity * 255.0));
        for (int y = 0; y < height; ++y) {
            for (int x = 0; x < width; ++x) {
                const std::uint8_t coverage =
                    plate_coverage(x, y, width, height, radius);
                if (coverage == 0 || opacity == 0) {
                    continue;
                }
                const int alpha = coverage * clamp_byte(opacity) / 255;
                std::uint8_t* pixel =
                    pixels + (static_cast<std::size_t>(y) * width + x) * 4;
                pixel[0] = clamp_byte(layout.background.color.b * alpha / 255);
                pixel[1] = clamp_byte(layout.background.color.g * alpha / 255);
                pixel[2] = clamp_byte(layout.background.color.r * alpha / 255);
                pixel[3] = static_cast<std::uint8_t>(alpha);
            }
        }
    }

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
        const int outline = std::max(0, run.style.outline_px);
        const int x0 = run.x - outline;
        const int y0 = run.y - outline;
        const int x1 = run.x + std::max(1, run.width) + outline;
        const int y1 = run.y + std::max(1, run.height) + outline;
        clear_rect(mask, x0, y0, x1, y1);

        text.select_font(mask.dc, run.style.metrics);
        TextOutW(
            mask.dc, run.x, run.y, wide.c_str(), static_cast<int>(wide.size()));

        const std::uint8_t opacity =
            clamp_byte(static_cast<int>(std::lround(run.style.opacity * 255.0)));

        // Outline first, sampled from the glyph mask at a one-ring offset.
        if (outline > 0) {
            for (int y = std::max(0, y0); y < std::min(height, y1); ++y) {
                for (int x = std::max(0, x0); x < std::min(width, x1); ++x) {
                    std::uint8_t coverage = 0;
                    for (int dy = -outline; dy <= outline; ++dy) {
                        for (int dx = -outline; dx <= outline; ++dx) {
                            if (dx * dx + dy * dy > outline * outline) {
                                continue;
                            }
                            coverage = std::max(
                                coverage, mask_alpha(mask, x - dx, y - dy));
                        }
                    }
                    if (coverage == 0) {
                        continue;
                    }
                    std::uint8_t* pixel =
                        pixels + (static_cast<std::size_t>(y) * width + x) * 4;
                    blend(pixel, coverage, opacity, run.style.outline_color);
                }
            }
        }

        for (int y = std::max(0, run.y); y < std::min(height, run.y + run.height); ++y) {
            for (int x = std::max(0, run.x);
                 x < std::min(width, run.x + std::max(1, run.width));
                 ++x) {
                const std::uint8_t coverage = mask_alpha(mask, x, y);
                if (coverage == 0) {
                    continue;
                }
                std::uint8_t* pixel =
                    pixels + (static_cast<std::size_t>(y) * width + x) * 4;
                blend(pixel, coverage, opacity, run.style.color);
            }
        }
        clear_rect(mask, x0, y0, x1, y1);
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
