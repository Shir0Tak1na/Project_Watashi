#include "watashi/ui/control_window.hpp"

#include <iostream>
#include <utility>

#include "watashi/ui/actions.hpp"

namespace watashi::ui {
namespace {

constexpr wchar_t kClassName[] = L"WatashiControlWindow";
constexpr wchar_t kWindowTitle[] = L"watashi control";
constexpr int kWindowWidth = 360;
constexpr int kWindowHeight = 76;
constexpr int kButtonHeight = 28;
constexpr int kButtonWidth = 108;
constexpr int kMargin = 8;
constexpr int kEdgeMargin = 24;

bool class_registered = false;

bool ensure_class(HINSTANCE instance) {
    if (class_registered) {
        return true;
    }
    WNDCLASSEXW window_class{};
    window_class.cbSize = sizeof(window_class);
    window_class.style = CS_HREDRAW | CS_VREDRAW;
    window_class.lpfnWndProc = &ControlWindow::window_proc;
    window_class.hInstance = instance;
    window_class.lpszClassName = kClassName;
    if (RegisterClassExW(&window_class) == 0) {
        return false;
    }
    class_registered = true;
    return true;
}

}  // namespace

ControlWindow::~ControlWindow() {
    if (hwnd_ != nullptr) {
        DestroyWindow(hwnd_);
        hwnd_ = nullptr;
    }
}

bool ControlWindow::create(HINSTANCE instance, ActionHandler handler) {
    if (hwnd_ != nullptr) {
        return true;
    }
    if (!ensure_class(instance)) {
        std::cerr << "control: RegisterClassExW failed, error " << GetLastError()
                  << "\n";
        return false;
    }
    handler_ = std::move(handler);

    // WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU: a normal window. Deliberately no
    // WS_EX_NOACTIVATE and no WS_EX_TOOLWINDOW, so the buttons accept clicks.
    hwnd_ = CreateWindowExW(
        0,
        kClassName,
        kWindowTitle,
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU,
        CW_USEDEFAULT,
        CW_USEDEFAULT,
        kWindowWidth,
        kWindowHeight,
        nullptr,
        nullptr,
        instance,
        this);
    if (hwnd_ == nullptr) {
        std::cerr << "control: CreateWindowExW failed, error " << GetLastError()
                  << "\n";
        return false;
    }
    update(false, true);
    return true;
}

void ControlWindow::show() {
    if (hwnd_ == nullptr) {
        return;
    }
    ShowWindow(hwnd_, SW_SHOWNOACTIVATE);
    visible_ = true;
}

void ControlWindow::hide() {
    if (hwnd_ == nullptr) {
        return;
    }
    ShowWindow(hwnd_, SW_HIDE);
    visible_ = false;
}

void ControlWindow::toggle() {
    if (visible_) {
        hide();
    } else {
        show();
    }
}

bool ControlWindow::visible() const {
    return hwnd_ != nullptr && IsWindowVisible(hwnd_);
}

void ControlWindow::update(bool paused, bool overlay_visible) {
    if (pause_button_ != nullptr) {
        SetWindowTextW(pause_button_, paused ? L"Resume" : L"Pause");
    }
    if (visibility_button_ != nullptr) {
        SetWindowTextW(visibility_button_, overlay_visible ? L"Hide" : L"Show");
    }
}

LRESULT CALLBACK ControlWindow::window_proc(HWND hwnd, UINT message,
                                            WPARAM wparam, LPARAM lparam) {
    if (message == WM_NCCREATE) {
        auto* create = reinterpret_cast<CREATESTRUCTW*>(lparam);
        SetWindowLongPtrW(hwnd, GWLP_USERDATA,
                          reinterpret_cast<LONG_PTR>(create->lpCreateParams));
    }
    auto* self = reinterpret_cast<ControlWindow*>(
        GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    if (self != nullptr) {
        // WM_NCCREATE and WM_CREATE run inside CreateWindowExW, before the
        // member is assigned by the caller; bind it here so DefWindowProc and
        // the child-button creation see the real HWND.
        self->hwnd_ = hwnd;
        return self->handle_message(message, wparam, lparam);
    }
    return DefWindowProcW(hwnd, message, wparam, lparam);
}

LRESULT ControlWindow::handle_message(UINT message, WPARAM wparam,
                                      LPARAM lparam) {
    switch (message) {
        case WM_CREATE: {
            HINSTANCE instance = reinterpret_cast<HINSTANCE>(GetWindowLongPtrW(
                hwnd_, GWLP_HINSTANCE));
            const int y = kMargin;
            pause_button_ = CreateWindowW(
                L"BUTTON", L"Pause", WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
                kMargin, y, kButtonWidth, kButtonHeight, hwnd_,
                reinterpret_cast<HMENU>(static_cast<UINT_PTR>(kTrayCommandPause)),
                instance, nullptr);
            visibility_button_ = CreateWindowW(
                L"BUTTON", L"Hide", WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
                kMargin * 2 + kButtonWidth, y, kButtonWidth, kButtonHeight,
                hwnd_,
                reinterpret_cast<HMENU>(
                    static_cast<UINT_PTR>(kTrayCommandVisibility)),
                instance, nullptr);
            exit_button_ = CreateWindowW(
                L"BUTTON", L"Exit", WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
                kMargin * 3 + kButtonWidth * 2, y, kButtonWidth, kButtonHeight,
                hwnd_,
                reinterpret_cast<HMENU>(static_cast<UINT_PTR>(kTrayCommandExit)),
                instance, nullptr);

            RECT work{};
            if (SystemParametersInfoW(SPI_GETWORKAREA, 0, &work, 0)) {
                SetWindowPos(
                    hwnd_, nullptr,
                    work.right - kWindowWidth - kEdgeMargin,
                    work.bottom - kWindowHeight - kEdgeMargin,
                    kWindowWidth, kWindowHeight,
                    SWP_NOZORDER | SWP_NOACTIVATE);
            }
            return 0;
        }
        case WM_COMMAND: {
            const ControlAction action =
                control_action_from_id(static_cast<int>(LOWORD(wparam)));
            if (action != ControlAction::kNone && handler_) {
                handler_(action);
            }
            return 0;
        }
        case WM_CLOSE:
            // The close button hides the control surface; it never exits the
            // overlay. Exit is an explicit button or hotkey.
            hide();
            return 0;
        case WM_DESTROY:
            pause_button_ = nullptr;
            visibility_button_ = nullptr;
            exit_button_ = nullptr;
            hwnd_ = nullptr;
            return 0;
        default:
            break;
    }
    return DefWindowProcW(hwnd_, message, wparam, lparam);
}

}  // namespace watashi::ui
