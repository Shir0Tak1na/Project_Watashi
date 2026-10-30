// Software rendering for the layered window: GDI text, pixels done by hand.
//
// A layered window needs **premultiplied** BGRA: every pixel is a colour already
// multiplied by its alpha. GDI cannot write that (its text is coverage on an
// opaque surface), so text is drawn into a greyscale mask first and composited
// here. The result is one buffer that ``UpdateLayeredWindow`` accepts as-is.
//
// The plate is drawn directly into the pixel buffer, which keeps the blended
// background identical on every machine and makes the output reproducible.

#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <utility>
#include <vector>

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include "watashi/ui/layout.hpp"

namespace watashi::ui {

/// Premultiplied BGRA, top-down. Row 0 is the top of the overlay.
struct OverlayImage {
    int width = 0;
    int height = 0;
    std::vector<std::uint8_t> bgra;

    [[nodiscard]] bool empty() const noexcept {
        return width <= 0 || height <= 0 || bgra.empty();
    }
};

struct Theme {
    std::uint8_t plate_r = 14;
    std::uint8_t plate_g = 16;
    std::uint8_t plate_b = 22;
    /// 0..255. A little translucency keeps the subtitle readable over bright
    /// scenes without hiding the scene itself.
    std::uint8_t plate_alpha = 190;
    std::uint8_t source_r = 198;
    std::uint8_t source_g = 205;
    std::uint8_t source_b = 216;
    std::uint8_t target_r = 246;
    std::uint8_t target_g = 247;
    std::uint8_t target_b = 250;
    std::uint8_t status_r = 236;
    std::uint8_t status_g = 196;
    std::uint8_t status_b = 112;
};

/// One GDI measurement/drawing context. Fonts are cached per size and weight,
/// because a repaint happens on every new subtitle and creating a font per run
/// would allocate on the UI thread.
class GdiText {
public:
    GdiText();
    ~GdiText();

    GdiText(const GdiText&) = delete;
    GdiText& operator=(const GdiText&) = delete;

    [[nodiscard]] TextExtent measure(const std::string& utf8, int px);

    /// Selects (and caches) a font into ``dc``. ``bold`` is used for the target
    /// line, which is the text the user actually reads.
    HFONT select_font(HDC dc, int px, bool bold);

    [[nodiscard]] TextMeasure as_measure();

private:
    HDC dc_ = nullptr;
    std::map<std::pair<int, bool>, HFONT> fonts_;
};

/// Compose a premultiplied BGRA image for ``layout``.
[[nodiscard]] OverlayImage compose_overlay(
    const Layout& layout,
    GdiText& text,
    const Theme& theme = {});

/// Write a screenshot. The alpha channel is flattened over a dark background so
/// the BMP is readable in any viewer; screenshots are evidence, not assets.
bool write_bmp(const std::string& path, const OverlayImage& image);

}  // namespace watashi::ui
