#include "watashi/ui/layered_window.hpp"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace watashi::ui {
namespace {

constexpr wchar_t kClassName[] = L"WatashiOverlayWindow";

bool class_registered = false;

void register_class() {
    if (class_registered) {
        return;
    }
    WNDCLASSEXW window_class{};
    window_class.cbSize = sizeof(window_class);
    window_class.style = 0;
    window_class.lpfnWndProc = &LayeredWindow::window_proc;
    window_class.hInstance = GetModuleHandleW(nullptr);
    window_class.hCursor = nullptr;
    window_class.lpszClassName = kClassName;
    RegisterClassExW(&window_class);
    class_registered = true;
}

}  // namespace

WindowStyles overlay_styles() noexcept {
    WindowStyles styles;
    styles.style = WS_POPUP;
    styles.extended_style = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST |
                            WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW;
    return styles;
}

LayeredWindow::~LayeredWindow() {
    destroy();
}

bool LayeredWindow::create() {
    register_class();
    const WindowStyles styles = overlay_styles();
    hwnd_ = CreateWindowExW(
        styles.extended_style,
        kClassName,
        L"watashi overlay",
        styles.style,
        -32000, -32000, 1, 1,
        nullptr,
        nullptr,
        GetModuleHandleW(nullptr),
        nullptr);
    if (hwnd_ == nullptr) {
        last_error_ = GetLastError();
        return false;
    }
    // Show without activating; WS_EX_NOACTIVATE keeps focus where it was.
    ShowWindow(hwnd_, SW_SHOWNOACTIVATE);
    return true;
}

void LayeredWindow::destroy() {
    if (memory_dc_ != nullptr) {
        DeleteDC(memory_dc_);
        memory_dc_ = nullptr;
    }
    if (bitmap_ != nullptr) {
        DeleteObject(bitmap_);
        bitmap_ = nullptr;
    }
    bits_ = nullptr;
    bitmap_width_ = 0;
    bitmap_height_ = 0;
    if (hwnd_ != nullptr) {
        DestroyWindow(hwnd_);
        hwnd_ = nullptr;
    }
}

void LayeredWindow::ensure_bitmap(int width, int height) {
    if (bitmap_ != nullptr && bitmap_width_ == width && bitmap_height_ == height) {
        return;
    }
    if (memory_dc_ != nullptr) {
        DeleteDC(memory_dc_);
        memory_dc_ = nullptr;
    }
    if (bitmap_ != nullptr) {
        DeleteObject(bitmap_);
        bitmap_ = nullptr;
    }
    bitmap_width_ = 0;
    bitmap_height_ = 0;
    bits_ = nullptr;

    BITMAPINFO info{};
    info.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    info.bmiHeader.biWidth = width;
    info.bmiHeader.biHeight = -height;  // top-down
    info.bmiHeader.biPlanes = 1;
    info.bmiHeader.biBitCount = 32;
    info.bmiHeader.biCompression = BI_RGB;

    HDC screen = GetDC(nullptr);
    bitmap_ = CreateDIBSection(screen, &info, DIB_RGB_COLORS, &bits_, nullptr, 0);
    ReleaseDC(nullptr, screen);
    if (bitmap_ != nullptr) {
        memory_dc_ = CreateCompatibleDC(nullptr);
        if (memory_dc_ != nullptr) {
            SelectObject(memory_dc_, bitmap_);
        }
    }
    if (bitmap_ != nullptr && memory_dc_ != nullptr && bits_ != nullptr) {
        bitmap_width_ = width;
        bitmap_height_ = height;
    }
}

void LayeredWindow::present(const OverlayImage& image, int x, int y) {
    if (hwnd_ == nullptr || image.empty()) {
        return;
    }
    ensure_bitmap(image.width, image.height);
    if (bitmap_ == nullptr || memory_dc_ == nullptr || bits_ == nullptr) {
        return;
    }

    std::memcpy(
        bits_,
        image.bgra.data(),
        static_cast<std::size_t>(image.width) * image.height * 4);

    HDC screen = GetDC(nullptr);
    POINT destination{x, y};
    SIZE size{image.width, image.height};
    POINT source{0, 0};
    BLENDFUNCTION blend{};
    blend.BlendOp = AC_SRC_OVER;
    blend.SourceConstantAlpha = 255;
    blend.AlphaFormat = AC_SRC_ALPHA;

    UpdateLayeredWindow(
        hwnd_,
        screen,
        &destination,
        &size,
        memory_dc_,
        &source,
        0,
        &blend,
        ULW_ALPHA);
    ReleaseDC(nullptr, screen);
}

bool LayeredWindow::take_dpi_changed(int& dpi) {
    if (pending_dpi_ == 0) {
        return false;
    }
    dpi = pending_dpi_;
    pending_dpi_ = 0;
    return true;
}

LRESULT CALLBACK LayeredWindow::window_proc(
    HWND hwnd,
    UINT message,
    WPARAM wparam,
    LPARAM lparam) {
    LayeredWindow* self =
        reinterpret_cast<LayeredWindow*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    if (message == WM_NCCREATE) {
        const auto* create = reinterpret_cast<CREATESTRUCTW*>(lparam);
        self = static_cast<LayeredWindow*>(create->lpCreateParams);
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(self));
    }
    if (self != nullptr) {
        return self->handle(hwnd, message, wparam, lparam);
    }
    return DefWindowProcW(hwnd, message, wparam, lparam);
}

LRESULT LayeredWindow::handle(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam) {
    switch (message) {
        case WM_NCHITTEST:
            // Clicks, drags and wheel events belong to the window underneath.
            return HTTRANSPARENT;
        case WM_MOUSEACTIVATE:
            return MA_NOACTIVATE;
        case WM_ERASEBKGND:
            return 1;
        case WM_DPICHANGED:
            pending_dpi_ = static_cast<int>(HIWORD(wparam));
            return 0;
        case WM_DESTROY:
            hwnd_ = nullptr;
            return 0;
        default:
            break;
    }
    return DefWindowProcW(hwnd, message, wparam, lparam);
}

}  // namespace watashi::ui
