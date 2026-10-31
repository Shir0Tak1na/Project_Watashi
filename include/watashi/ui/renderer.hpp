// Software rendering for the layered window: GDI text, pixels done by hand.
//
// A layered window needs **premultiplied** BGRA: every pixel is a colour already
// multiplied by its alpha. GDI cannot write that (its text is coverage on an
// opaque surface), so text is drawn into a greyscale mask first and composited
// here. The result is one buffer that ``UpdateLayeredWindow`` accepts as-is.
//
// The plate and its colour come from the presentation spec (layout.background);
// per-run colour, opacity and outline come from the run style, so a spec change
// visibly changes the buffer and not just the stored state.

#pragma once

#include <cstdint>
#include <map>
#include <string>
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

/// One GDI measurement/drawing context. Fonts are cached per face, size,
/// weight and slant; a repaint happens on every new subtitle, so creating a
/// font per run would allocate on the UI thread.
class GdiText {
public:
    GdiText();
    ~GdiText();

    GdiText(const GdiText&) = delete;
    GdiText& operator=(const GdiText&) = delete;

    [[nodiscard]] TextExtent measure(
        const std::string& utf8,
        const MeasureStyle& style);

    /// Selects (and caches) the font ``style`` describes into ``dc``.
    HFONT select_font(HDC dc, const MeasureStyle& style);

    [[nodiscard]] TextMeasure as_measure();

private:
    struct FontKey {
        std::string family;
        int px = 0;
        bool bold = false;
        bool italic = false;

        [[nodiscard]] bool operator<(const FontKey& other) const;
    };

    HDC dc_ = nullptr;
    std::map<FontKey, HFONT> fonts_;
};

/// Compose the premultiplied BGRA image the layout describes.
[[nodiscard]] OverlayImage compose_overlay(const Layout& layout, GdiText& text);

/// Write a screenshot. The alpha channel is flattened over a dark background so
/// the BMP is readable in any viewer; screenshots are evidence, not assets.
bool write_bmp(const std::string& path, const OverlayImage& image);

}  // namespace watashi::ui
