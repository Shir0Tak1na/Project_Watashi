// The overlay window itself: a borderless, always-on-top, click-through
// layered window drawn with per-pixel alpha.
//
// The five extended styles are the contract of this window, not decoration:
//
//   WS_EX_LAYERED     per-pixel alpha through UpdateLayeredWindow
//   WS_EX_TRANSPARENT clicks fall through to whatever is below
//   WS_EX_TOPMOST     stays above game/window content
//   WS_EX_NOACTIVATE  a subtitle must never take focus from the game
//   WS_EX_TOOLWINDOW  no taskbar entry, no Alt-Tab entry
//
// WM_NCHITTEST also answers HTTRANSPARENT and WM_MOUSEACTIVATE answers
// MA_NOACTIVATE. That is belt and braces: the styles are what Windows consults
// first, the messages cover the compositor paths where the style is not enough.

#pragma once

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include "watashi/ui/renderer.hpp"

namespace watashi::ui {

struct WindowStyles {
    DWORD style = 0;
    DWORD extended_style = 0;
};

/// Pure, so the unit tests can assert every required bit without creating a
/// window.
[[nodiscard]] WindowStyles overlay_styles() noexcept;

class LayeredWindow {
public:
    LayeredWindow() = default;
    ~LayeredWindow();

    LayeredWindow(const LayeredWindow&) = delete;
    LayeredWindow& operator=(const LayeredWindow&) = delete;

    /// Creates and shows the window. False means there is no desktop session
    /// (CreateWindowExW failed); ``last_error`` carries the Win32 code.
    bool create();
    void destroy();

    /// Moves and repaints in one call. ``x``/``y`` are physical screen pixels.
    /// Does not activate the window.
    void present(const OverlayImage& image, int x, int y);

    /// True once WM_DPICHANGED has been received; the caller re-lays-out.
    bool take_dpi_changed(int& dpi);

    [[nodiscard]] HWND hwnd() const noexcept { return hwnd_; }
    [[nodiscard]] DWORD last_error() const noexcept { return last_error_; }

    /// The class proc has to be reachable from the class registration helper.
    static LRESULT CALLBACK window_proc(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam);

private:
    LRESULT handle(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam);
    void ensure_bitmap(int width, int height);

    HWND hwnd_ = nullptr;
    HBITMAP bitmap_ = nullptr;
    HDC memory_dc_ = nullptr;
    void* bits_ = nullptr;
    int bitmap_width_ = 0;
    int bitmap_height_ = 0;
    DWORD last_error_ = 0;
    int pending_dpi_ = 0;
};

}  // namespace watashi::ui
