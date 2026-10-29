#include "watashi/capture.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cwctype>
#include <iterator>
#include <string>

#if defined(_WIN32)
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#if __has_include(<dwmapi.h>)
#include <dwmapi.h>
#define WATASHI_HAS_DWMAPI 1
#endif
#endif

namespace watashi {
namespace {

double steady_seconds() {
    return std::chrono::duration<double>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

std::string describe(const std::string& mode, const CaptureRect& rect) {
    return mode + " " + std::to_string(rect.width) + "x" + std::to_string(rect.height) + "+" +
           std::to_string(rect.x) + "+" + std::to_string(rect.y);
}

#if defined(_WIN32)

/// Copies a 32bpp top-down DIB into the packed BGR layout ImageView expects.
void copy_dib_to_bgr(const std::uint8_t* bits, int width, int height,
                     std::vector<std::uint8_t>& out) {
    out.resize(static_cast<std::size_t>(width) * static_cast<std::size_t>(height) * 3);
    for (int y = 0; y < height; ++y) {
        const std::uint8_t* source =
            bits + static_cast<std::size_t>(y) * static_cast<std::size_t>(width) * 4;
        std::uint8_t* target =
            out.data() + static_cast<std::size_t>(y) * static_cast<std::size_t>(width) * 3;
        for (int x = 0; x < width; ++x) {
            const std::uint8_t* pixel = source + static_cast<std::size_t>(x) * 4;
            target[0] = pixel[0];
            target[1] = pixel[1];
            target[2] = pixel[2];
            target += 3;
        }
    }
}

/// BitBlt a rectangle from ``source`` (window origin) or ``screen`` into a
/// top-down DIB and return its BGR bytes.
std::vector<std::uint8_t> blit_region(
    HDC source_dc,
    int source_x,
    int source_y,
    int width,
    int height) {
    BITMAPINFO info{};
    info.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    info.bmiHeader.biWidth = width;
    info.bmiHeader.biHeight = -height;  // negative: top-down rows
    info.bmiHeader.biPlanes = 1;
    info.bmiHeader.biBitCount = 32;
    info.bmiHeader.biCompression = BI_RGB;

    void* bits = nullptr;
    HBITMAP bitmap = CreateDIBSection(source_dc, &info, DIB_RGB_COLORS, &bits, nullptr, 0);
    if (bitmap == nullptr || bits == nullptr) {
        throw CaptureError("CreateDIBSection failed");
    }

    HDC memory = CreateCompatibleDC(source_dc);
    if (memory == nullptr) {
        DeleteObject(bitmap);
        throw CaptureError("CreateCompatibleDC failed");
    }

    HGDIOBJ previous = SelectObject(memory, bitmap);
    // Plain SRCCOPY on purpose: CAPTUREBLT asks the driver to include layered
    // windows and measured at 260 ms per 1200x200 grab on this host, which
    // would blow the 10 ms capture budget by itself.
    const BOOL copied = BitBlt(
        memory, 0, 0, width, height, source_dc, source_x, source_y, SRCCOPY);
    SelectObject(memory, previous);
    DeleteDC(memory);

    std::vector<std::uint8_t> bytes;
    if (copied) {
        copy_dib_to_bgr(static_cast<const std::uint8_t*>(bits), width, height, bytes);
    }
    DeleteObject(bitmap);

    if (!copied) {
        throw CaptureError("BitBlt failed");
    }
    return bytes;
}

std::wstring to_wide(std::string_view text) {
    if (text.empty()) {
        return {};
    }
    const int length = MultiByteToWideChar(
        CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0);
    std::wstring wide(static_cast<std::size_t>(length), L'\0');
    MultiByteToWideChar(
        CP_UTF8, 0, text.data(), static_cast<int>(text.size()), wide.data(), length);
    return wide;
}

std::string to_utf8(const std::wstring& wide) {
    if (wide.empty()) {
        return {};
    }
    const int length = WideCharToMultiByte(
        CP_UTF8, 0, wide.data(), static_cast<int>(wide.size()), nullptr, 0, nullptr, nullptr);
    std::string text(static_cast<std::size_t>(length), '\0');
    WideCharToMultiByte(
        CP_UTF8, 0, wide.data(), static_cast<int>(wide.size()), text.data(), length, nullptr, nullptr);
    return text;
}

std::wstring lower_wide(std::wstring text) {
    for (auto& character : text) {
        character = static_cast<wchar_t>(towlower(character));
    }
    return text;
}

struct TitleSearch {
    std::wstring needle;
    HWND found = nullptr;
    std::size_t best_length = 0;
};

BOOL CALLBACK enum_window(HWND window, LPARAM context) {
    auto* search = reinterpret_cast<TitleSearch*>(context);
    if (!IsWindowVisible(window)) {
        return TRUE;
    }
    const std::wstring title =
        lower_wide(to_wide(window_title(reinterpret_cast<std::uintptr_t>(window))));
    if (title.empty()) {
        return TRUE;
    }
    if (title.find(search->needle) == std::wstring::npos) {
        return TRUE;
    }
    if (search->found == nullptr || title.size() < search->best_length) {
        search->found = window;
        search->best_length = title.size();
    }
    return TRUE;
}

CaptureRect dwm_frame_bounds(HWND window) {
#if defined(WATASHI_HAS_DWMAPI)
    RECT bounds{};
    const HRESULT status = DwmGetWindowAttribute(
        window, DWMWA_EXTENDED_FRAME_BOUNDS, &bounds, sizeof(bounds));
    if (SUCCEEDED(status)) {
        return capture_detail::make_rect(bounds.left, bounds.top, bounds.right, bounds.bottom);
    }
#endif
    RECT fallback{};
    if (GetWindowRect(window, &fallback)) {
        return capture_detail::make_rect(
            fallback.left, fallback.top, fallback.right, fallback.bottom);
    }
    return {};
}

/// Owns a screen or window DC for the duration of one grab.
class DcHandle {
public:
    DcHandle(HWND window, bool whole_window) : owner_(window) {
        dc_ = whole_window ? GetWindowDC(window) : GetDC(window);
    }
    ~DcHandle() {
        if (dc_ != nullptr) {
            ReleaseDC(owner_, dc_);
        }
    }
    DcHandle(const DcHandle&) = delete;
    DcHandle& operator=(const DcHandle&) = delete;

    [[nodiscard]] HDC get() const noexcept { return dc_; }

private:
    HWND owner_ = nullptr;
    HDC dc_ = nullptr;
};

#endif  // _WIN32

}  // namespace

namespace capture_detail {

bool rect_empty(const CaptureRect& rect) noexcept {
    return rect.width <= 0 || rect.height <= 0;
}

CaptureRect make_rect(int left, int top, int right, int bottom) {
    return CaptureRect{left, top, right - left, bottom - top};
}

CaptureRect clip_rect(const CaptureRect& wanted, const CaptureRect& bounds) {
    if (rect_empty(wanted) || rect_empty(bounds)) {
        return {};
    }
    const int left = std::max(wanted.x, bounds.x);
    const int top = std::max(wanted.y, bounds.y);
    const int right = std::min(wanted.right(), bounds.right());
    const int bottom = std::min(wanted.bottom(), bounds.bottom());
    if (left >= right || top >= bottom) {
        return {};
    }
    return make_rect(left, top, right, bottom);
}

CaptureRect scale_rect(const CaptureRect& logical, double scale) {
    if (scale <= 0.0) {
        return {};
    }
    const auto scaled = [scale](int value) {
        return static_cast<int>(std::lround(static_cast<double>(value) * scale));
    };
    return CaptureRect{
        scaled(logical.x),
        scaled(logical.y),
        scaled(logical.width),
        scaled(logical.height),
    };
}

double dpi_scale(int dpi) noexcept {
    if (dpi <= 0) {
        return 1.0;
    }
    return static_cast<double>(dpi) / 96.0;
}

CaptureRect primary_monitor_rect() {
#if defined(_WIN32)
    const int width = GetSystemMetrics(SM_CXSCREEN);
    const int height = GetSystemMetrics(SM_CYSCREEN);
    return CaptureRect{0, 0, width, height};
#else
    return {};
#endif
}

CaptureRect virtual_screen_rect() {
#if defined(_WIN32)
    return make_rect(
        GetSystemMetrics(SM_XVIRTUALSCREEN),
        GetSystemMetrics(SM_YVIRTUALSCREEN),
        GetSystemMetrics(SM_XVIRTUALSCREEN) + GetSystemMetrics(SM_CXVIRTUALSCREEN),
        GetSystemMetrics(SM_YVIRTUALSCREEN) + GetSystemMetrics(SM_CYVIRTUALSCREEN));
#else
    return {};
#endif
}

}  // namespace capture_detail

bool enable_per_monitor_dpi() {
#if defined(_WIN32)
    // PerMonitorV2 exists from Windows 10 1703. The context is -4 by value and
    // the symbol is loaded dynamically so this builds against older SDKs.
    using SetContext = BOOL(WINAPI*)(HANDLE);
    if (HMODULE user32 = GetModuleHandleW(L"user32.dll")) {
        auto set_context =
            reinterpret_cast<SetContext>(GetProcAddress(user32, "SetProcessDpiAwarenessContext"));
        if (set_context != nullptr) {
            const HANDLE per_monitor_v2 = reinterpret_cast<HANDLE>(static_cast<std::intptr_t>(-4));
            if (set_context(per_monitor_v2)) {
                return true;
            }
        }
    }
    // Windows 8.1: PROCESS_PER_MONITOR_DPI_AWARE == 2.
    using SetAwareness = HRESULT(WINAPI*)(int);
    if (HMODULE shcore = LoadLibraryW(L"shcore.dll")) {
        auto set_awareness =
            reinterpret_cast<SetAwareness>(GetProcAddress(shcore, "SetProcessDpiAwareness"));
        const bool ok = set_awareness != nullptr && set_awareness(2) == S_OK;
        FreeLibrary(shcore);
        if (ok) {
            return true;
        }
    }
    // Vista and later; system-DPI awareness is the best that is left.
    using SetSystem = BOOL(WINAPI*)();
    if (HMODULE user32 = GetModuleHandleW(L"user32.dll")) {
        auto set_system =
            reinterpret_cast<SetSystem>(GetProcAddress(user32, "SetProcessDPIAware"));
        if (set_system != nullptr && set_system()) {
            return true;
        }
    }
#endif
    return false;
}

std::uintptr_t find_window_by_title(std::string_view needle) {
#if defined(_WIN32)
    if (needle.empty()) {
        return 0;
    }
    TitleSearch search;
    search.needle = lower_wide(to_wide(needle));
    EnumWindows(enum_window, reinterpret_cast<LPARAM>(&search));
    return reinterpret_cast<std::uintptr_t>(search.found);
#else
    (void)needle;
    return 0;
#endif
}

std::string window_title(std::uintptr_t hwnd) {
#if defined(_WIN32)
    const HWND window = reinterpret_cast<HWND>(hwnd);
    if (window == nullptr || !IsWindow(window)) {
        return {};
    }
    wchar_t buffer[512];
    const int length = GetWindowTextW(window, buffer, static_cast<int>(std::size(buffer)));
    if (length <= 0) {
        return {};
    }
    return to_utf8(std::wstring(buffer, static_cast<std::size_t>(length)));
#else
    (void)hwnd;
    return {};
#endif
}

ScreenCapture::ScreenCapture(Options options) {
    region_ = options.has_region ? options.region : capture_detail::primary_monitor_rect();
    if (capture_detail::rect_empty(region_)) {
        throw CaptureError(
            options.has_region ? "the requested capture region is empty"
                               : "no display is available");
    }
}

CaptureFrame ScreenCapture::grab() {
    CaptureFrame frame;
    frame.t_start_s = steady_seconds();

#if defined(_WIN32)
    const CaptureRect bounds = capture_detail::virtual_screen_rect();
    const CaptureRect visible = capture_detail::clip_rect(region_, bounds);
    if (capture_detail::rect_empty(visible)) {
        throw CaptureError(
            "capture region " + describe("region", region_) +
            " does not overlap the virtual screen " + describe("screen", bounds));
    }

    DcHandle screen(nullptr, false);
    if (screen.get() == nullptr) {
        throw CaptureError("GetDC(NULL) failed");
    }

    frame.bgr = blit_region(screen.get(), visible.x, visible.y, visible.width, visible.height);
    frame.width = visible.width;
    frame.height = visible.height;
    frame.origin_x = visible.x;
    frame.origin_y = visible.y;
#else
    throw CaptureError("screen capture is only implemented on Windows");
#endif

    frame.t_end_s = steady_seconds();
    return frame;
}

CaptureInfo ScreenCapture::info() const {
    return CaptureInfo{"screen", region_, describe("screen", region_)};
}

WindowCapture::WindowCapture(Options options) : hwnd_(options.hwnd) {
#if defined(_WIN32)
    const HWND window = reinterpret_cast<HWND>(hwnd_);
    if (window == nullptr || !IsWindow(window)) {
        throw CaptureError("window handle is not a window");
    }
    title_ = window_title(hwnd_);
    initial_ = dwm_frame_bounds(window);
    if (capture_detail::rect_empty(initial_)) {
        throw CaptureError("window has no usable bounds");
    }
#else
    throw CaptureError("window capture is only implemented on Windows");
#endif
}

CaptureFrame WindowCapture::grab() {
    CaptureFrame frame;
    frame.t_start_s = steady_seconds();

#if defined(_WIN32)
    const HWND window = reinterpret_cast<HWND>(hwnd_);
    if (window == nullptr || !IsWindow(window)) {
        throw CaptureError("window is gone (closed)");
    }
    if (IsIconic(window)) {
        throw CaptureError("window is minimized; nothing is on screen to capture");
    }
    if (!IsWindowVisible(window)) {
        throw CaptureError("window is hidden; nothing is on screen to capture");
    }

    const CaptureRect bounds = dwm_frame_bounds(window);
    if (capture_detail::rect_empty(bounds)) {
        throw CaptureError("window has no usable bounds");
    }

    // DWM's bounds exclude the invisible resize border that GetWindowRect
    // includes, so the BitBlt source has to be shifted by that difference or the
    // frame comes back a few pixels off.
    RECT window_rect{};
    int offset_x = 0;
    int offset_y = 0;
    if (GetWindowRect(window, &window_rect)) {
        offset_x = bounds.x - window_rect.left;
        offset_y = bounds.y - window_rect.top;
    }

    DcHandle window_dc(window, true);
    if (window_dc.get() == nullptr) {
        throw CaptureError("GetWindowDC failed");
    }

    frame.bgr = blit_region(window_dc.get(), offset_x, offset_y, bounds.width, bounds.height);
    frame.width = bounds.width;
    frame.height = bounds.height;
    frame.origin_x = bounds.x;
    frame.origin_y = bounds.y;
#else
    throw CaptureError("window capture is only implemented on Windows");
#endif

    frame.t_end_s = steady_seconds();
    return frame;
}

CaptureInfo WindowCapture::info() const {
    CaptureInfo info;
    info.mode = "window";
    info.region = initial_;
    info.description = describe("window", initial_) +
                       (title_.empty() ? std::string{} : " \"" + title_ + "\"");
    return info;
}

}  // namespace watashi
