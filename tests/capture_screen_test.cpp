// Real capture against the compositor, in one small executable.
//
// This is kept out of the Catch2 binary on purpose: with no desktop session
// there is nothing honest to assert, and a skipped test would look exactly like
// a passing one. Exit 3 says "no display", and ctest reports it as a failure --
// which is what an unverifiable screen path deserves.
//
// It covers what the pure tests cannot: that BitBlt returns the requested
// rectangle, that a window is followed when it moves, and that a minimised or
// destroyed window raises CaptureError instead of returning black pixels.

#include <windows.h>

#include <cstdio>
#include <cstdlib>
#include <string>

#include "watashi/capture.hpp"

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

/// Let USER32 process the messages a window needs to be painted and composed.
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

    const watashi::CaptureRect primary = watashi::capture_detail::primary_monitor_rect();
    const watashi::CaptureRect virtual_screen = watashi::capture_detail::virtual_screen_rect();
    if (primary.width <= 0 || primary.height <= 0 || virtual_screen.width <= 0) {
        std::fprintf(stderr, "no desktop session: no monitor reports bounds\n");
        return 3;
    }
    std::printf("desktop: %dx%d at %d,%d\n", virtual_screen.width, virtual_screen.height,
                virtual_screen.x, virtual_screen.y);

    // --- screen -----------------------------------------------------------
    const watashi::CaptureRect region{primary.width / 2 - 100, primary.height / 2 - 50, 200, 100};
    try {
        watashi::ScreenCapture screen({region, true});
        const auto frame = screen.grab();
        check(frame.width == region.width && frame.height == region.height,
              "screen capture returns the requested size");
        check(frame.origin_x == region.x && frame.origin_y == region.y,
              "screen capture reports the true origin");
        check(frame.bgr.size() ==
                  static_cast<std::size_t>(region.width) * region.height * 3,
              "screen capture returns packed BGR bytes");
        check(frame.t_end_s >= frame.t_start_s, "capture timestamps are monotonic");
        std::printf("capture_ms=%.2f\n", (frame.t_end_s - frame.t_start_s) * 1000.0);
    } catch (const std::exception& error) {
        std::fprintf(stderr, "FAIL screen capture threw: %s\n", error.what());
        ++failures;
    }

    try {
        const watashi::CaptureRect overrun{primary.right() - 20, primary.y + 10, 200, 100};
        watashi::ScreenCapture screen({overrun, true});
        const auto frame = screen.grab();
        check(frame.width == 20, "a region crossing the screen edge is clipped");
    } catch (const std::exception& error) {
        std::fprintf(stderr, "FAIL clipped screen capture threw: %s\n", error.what());
        ++failures;
    }

    // --- window -----------------------------------------------------------
    const wchar_t* title = L"watashi capture probe window";
    HWND window = CreateWindowExW(
        0, L"STATIC", title, WS_OVERLAPPEDWINDOW | WS_VISIBLE, 200, 200, 400, 300, nullptr,
        nullptr, nullptr, nullptr);
    if (window == nullptr) {
        std::fprintf(stderr, "FAIL cannot create the probe window (error %lu)\n",
                     GetLastError());
        return 1;
    }
    pump(400);

    const auto found = watashi::find_window_by_title("watashi capture probe");
    check(found == reinterpret_cast<std::uintptr_t>(window),
          "find_window_by_title returns the probe window");

    try {
        watashi::WindowCapture capture({reinterpret_cast<std::uintptr_t>(window)});
        const auto first = capture.grab();
        std::printf("window_capture_ms=%.2f\n",
                    (first.t_end_s - first.t_start_s) * 1000.0);
        check(first.width > 300 && first.height > 200,
              "window capture returns the window's frame");
        check(std::abs(first.origin_x - 200) < 40 && std::abs(first.origin_y - 200) < 60,
              "window capture reports DWM's frame origin");

        SetWindowPos(window, HWND_TOP, 300, 260, 400, 300, SWP_SHOWWINDOW);
        pump(400);
        const auto moved = capture.grab();
        check(moved.origin_x != first.origin_x || moved.origin_y != first.origin_y,
              "a moved window is followed between frames");
        check(capture.info().mode == "window", "window capture reports its mode");

        ShowWindow(window, SW_MINIMIZE);
        pump(300);
        bool raised = false;
        try {
            (void)capture.grab();
        } catch (const watashi::CaptureError&) {
            raised = true;
        }
        check(raised, "a minimised window raises CaptureError");
    } catch (const std::exception& error) {
        std::fprintf(stderr, "FAIL window capture threw: %s\n", error.what());
        ++failures;
    }

    SetWindowTextW(window, L"watashi capture probe (gone)");
    DestroyWindow(window);
    pump(200);
    {
        bool raised = false;
        try {
            watashi::WindowCapture capture({reinterpret_cast<std::uintptr_t>(window)});
            (void)capture.grab();
        } catch (const watashi::CaptureError&) {
            raised = true;
        }
        check(raised, "a destroyed window raises CaptureError");
    }

    if (failures != 0) {
        std::fprintf(stderr, "%d capture check(s) failed\n", failures);
        return 1;
    }
    std::printf("all screen and window checks passed\n");
    return 0;
}
