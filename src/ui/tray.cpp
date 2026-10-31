#include "watashi/ui/tray.hpp"

#include <shellapi.h>

#include <algorithm>
#include <string>
#include <vector>

#include "watashi/ui/actions.hpp"

namespace watashi::ui {
namespace {

constexpr UINT kIconId = 1;
constexpr int kOwnerSize = 1;
constexpr int kOffscreen = -32000;

bool class_registered = false;

LRESULT CALLBACK tray_window_proc(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam) {
    return DefWindowProcW(hwnd, message, wparam, lparam);
}

bool ensure_class() {
    if (class_registered) {
        return true;
    }
    WNDCLASSEXW window_class{};
    window_class.cbSize = sizeof(window_class);
    window_class.style = 0;
    window_class.lpfnWndProc = &tray_window_proc;
    window_class.hInstance = GetModuleHandleW(nullptr);
    window_class.lpszClassName = kTrayWindowClass;
    if (RegisterClassExW(&window_class) == 0) {
        return false;
    }
    class_registered = true;
    return true;
}

std::wstring widen(const std::string& text) {
    if (text.empty()) {
        return std::wstring();
    }
    const int length = MultiByteToWideChar(
        CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0);
    std::wstring wide(static_cast<std::size_t>(length), L'\0');
    if (length > 0) {
        MultiByteToWideChar(
            CP_UTF8, 0, text.data(), static_cast<int>(text.size()), wide.data(), length);
    }
    return wide;
}

NOTIFYICONDATAW make_icon(HWND owner) {
    NOTIFYICONDATAW data{};
    data.cbSize = sizeof(data);
    data.hWnd = owner;
    data.uID = kIconId;
    data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
    data.uCallbackMessage = kTrayCallbackMessage;
    data.hIcon = LoadIconW(nullptr, MAKEINTRESOURCEW(32512));  // IDI_APPLICATION
    const std::wstring tip = L"watashi overlay";
    std::copy(tip.begin(), tip.end(), data.szTip);
    return data;
}

HWND create_owner() {
    if (!ensure_class()) {
        return nullptr;
    }
    // A normal, activatable top-level window: no WS_EX_NOACTIVATE, no
    // WS_EX_TRANSPARENT. It is parked offscreen and shown without activation so
    // SetForegroundWindow can make it foreground when the menu opens.
    HWND owner = CreateWindowExW(
        WS_EX_TOOLWINDOW,
        kTrayWindowClass,
        kTrayWindowTitle,
        WS_POPUP,
        kOffscreen,
        kOffscreen,
        kOwnerSize,
        kOwnerSize,
        nullptr,
        nullptr,
        GetModuleHandleW(nullptr),
        nullptr);
    if (owner != nullptr) {
        ShowWindow(owner, SW_SHOWNOACTIVATE);
    }
    return owner;
}

}  // namespace

TrayIcon::~TrayIcon() {
    remove();
}

bool TrayIcon::add() {
    if (added_) {
        return true;
    }
    if (owner_ == nullptr) {
        owner_ = create_owner();
    }
    if (owner_ == nullptr) {
        return false;
    }
    NOTIFYICONDATAW data = make_icon(owner_);
    added_ = Shell_NotifyIconW(NIM_ADD, &data) != FALSE;
    last_error_ = added_ ? 0 : GetLastError();
    return added_;
}

void TrayIcon::remove() {
    if (added_ && owner_ != nullptr) {
        NOTIFYICONDATAW data = make_icon(owner_);
        Shell_NotifyIconW(NIM_DELETE, &data);
        added_ = false;
    }
    if (owner_ != nullptr) {
        DestroyWindow(owner_);
        owner_ = nullptr;
    }
}

int TrayIcon::show_menu(bool paused, bool visible) {
    if (!added_ || owner_ == nullptr) {
        return 0;
    }
    HMENU menu = CreatePopupMenu();
    if (menu == nullptr) {
        return 0;
    }
    for (const auto& entry : tray_menu(paused, visible)) {
        const std::wstring label = widen(entry.label);
        const UINT flags = MF_STRING | (entry.checked ? MF_CHECKED : 0);
        AppendMenuW(menu, flags, static_cast<UINT_PTR>(entry.id), label.c_str());
    }

    POINT cursor{};
    GetCursorPos(&cursor);
    // The popup only dismisses by clicking elsewhere while its owner is
    // foreground. The owner is the tray window on purpose: SetForegroundWindow
    // on the NOACTIVATE overlay always fails.
    SetForegroundWindow(owner_);
    const int chosen = TrackPopupMenu(
        menu,
        TPM_RETURNCMD | TPM_RIGHTBUTTON,
        cursor.x,
        cursor.y,
        0,
        owner_,
        nullptr);
    DestroyMenu(menu);
    PostMessageW(owner_, WM_NULL, 0, 0);
    return chosen;
}

}  // namespace watashi::ui
