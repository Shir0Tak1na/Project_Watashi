// The overlay window against the real compositor.
//
// Kept out of the Catch2 binary for the same reason capture_screen_test is:
// with no desktop session there is nothing honest to assert, and a skipped
// test would look green. Exit 3 means "no display"; ctest reports that as a
// failure.
//
// It covers what pure tests cannot: that the layered window really is
// click-through (WindowFromPoint at its centre lands on another window), that
// the five extended styles survive creation, that the window does not steal
// foreground focus, and that UpdateLayeredWindow places it exactly where the
// layout says.

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <cstdio>
#include <string>

#include "watashi/capture.hpp"
#include "watashi/ui/layered_window.hpp"
#include "watashi/ui/layout.hpp"
#include "watashi/ui/renderer.hpp"

namespace {

int failures = 0;

void check(bool condition, const char* what) {
    if (!condition) {
        std::fprintf(stderr, "FAIL %s\n", what);
        ++failures;
    } else {
        std::fprintf(stdout, "ok   %s\n", what);
    }
}

/// Shares the lock with the report tests in watashi_tests: ctest -j must not
/// run another topmost overlay while this test checks click-through and focus.
class DesktopTestLock {
public:
    DesktopTestLock() {
        handle_ = CreateMutexW(nullptr, FALSE, L"watashi-desktop-test-lock");
        if (handle_ != nullptr) {
            WaitForSingleObject(handle_, INFINITE);
        }
    }
    ~DesktopTestLock() {
        if (handle_ != nullptr) {
            ReleaseMutex(handle_);
            CloseHandle(handle_);
        }
    }

private:
    HANDLE handle_ = nullptr;
};

void pump(int milliseconds) {
    const DWORD deadline = GetTickCount() + static_cast<DWORD>(milliseconds);
    while (GetTickCount() < deadline) {
        MSG message{};
        while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
        Sleep(10);
    }
}

}  // namespace

int main() {
    watashi::enable_per_monitor_dpi();
    DesktopTestLock desktop;

    const watashi::CaptureRect primary = watashi::capture_detail::primary_monitor_rect();
    if (primary.width <= 0 || primary.height <= 0) {
        std::fprintf(stderr, "no desktop session: no monitor reports bounds\n");
        return 3;
    }

    const HWND foreground_before = GetForegroundWindow();

    watashi::ui::LayeredWindow window;
    if (!window.create()) {
        std::fprintf(stderr, "no desktop session: CreateWindowExW failed (%lu)\n",
                     window.last_error());
        return 3;
    }
    const HWND hwnd = window.hwnd();
    const HWND foreground_after_create = GetForegroundWindow();

    const DWORD required = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST |
                           WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW;
    const DWORD extended = static_cast<DWORD>(GetWindowLongPtrW(hwnd, GWL_EXSTYLE));
    std::printf("ex_style=0x%08lx\n", static_cast<unsigned long>(extended));
    check((extended & required) == required,
          "layered, transparent, topmost, no-activate and toolwindow bits are set");
    check((extended & WS_EX_TOOLWINDOW) != 0, "no taskbar entry");
    check(IsWindowVisible(hwnd), "the overlay is visible");

    const int dpi = GetDpiForWindow(hwnd);

    watashi::ui::GdiText text;
    watashi::ui::LayoutRequest request;
    request.text.source = "the sword intent of this sect is a myth";
    request.text.target = "本门剑意只是传说";
    request.text.status = "ui window test";
    request.work_area = primary;
    request.dpi = dpi;
    request.spec = watashi::ui::PresentationSpec::bar();
    const watashi::ui::Layout layout =
        watashi::ui::compute_layout(request, text.as_measure());
    const watashi::ui::OverlayImage image =
        watashi::ui::compose_overlay(layout, text);

    window.present(image, layout.x, layout.y);
    // Sample focus immediately after the overlay appears, before the 600 ms
    // pump: an unrelated application taking focus during the pump must not be
    // blamed on this window.
    const HWND foreground_after_present = GetForegroundWindow();
    pump(600);

    RECT rect{};
    GetWindowRect(hwnd, &rect);
    std::printf("window_rect=%ld,%ld %ldx%ld expected=%d,%d %dx%d dpi=%d\n",
                rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top,
                layout.x, layout.y, layout.width, layout.height, dpi);
    check(rect.left == layout.x && rect.top == layout.y,
          "UpdateLayeredWindow places the window at the layout position");
    check(rect.right - rect.left == layout.width &&
              rect.bottom - rect.top == layout.height,
          "the window is exactly the composed image size");

    POINT center{layout.x + layout.width / 2, layout.y + layout.height / 2};
    const HWND under = WindowFromPoint(center);
    std::printf("window_under_center=0x%llx overlay=0x%llx\n",
                static_cast<unsigned long long>(reinterpret_cast<std::uintptr_t>(under)),
                static_cast<unsigned long long>(reinterpret_cast<std::uintptr_t>(hwnd)));
    check(under != hwnd, "WindowFromPoint at the centre lands on another window");

    std::printf("foreground_before=0x%llx after_create=0x%llx after_present=0x%llx\n",
                static_cast<unsigned long long>(
                    reinterpret_cast<std::uintptr_t>(foreground_before)),
                static_cast<unsigned long long>(
                    reinterpret_cast<std::uintptr_t>(foreground_after_create)),
                static_cast<unsigned long long>(
                    reinterpret_cast<std::uintptr_t>(foreground_after_present)));
    check(foreground_after_create == foreground_before &&
              foreground_after_present == foreground_before,
          "showing the overlay does not change the foreground window");

    // A DPI-sized overlay must stay inside the monitor it anchors to.
    check(layout.x >= primary.x && layout.x + layout.width <= primary.right(),
          "the anchor keeps the overlay inside the monitor width");

    window.destroy();

    if (failures != 0) {
        std::fprintf(stderr, "%d overlay check(s) failed\n", failures);
        return 1;
    }
    std::printf("all overlay window checks passed\n");
    return 0;
}
