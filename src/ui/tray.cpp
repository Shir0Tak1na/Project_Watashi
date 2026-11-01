#include "watashi/ui/tray.hpp"

#include <shellapi.h>

#include <algorithm>
#include <cstdint>
#include <iostream>
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

NOTIFYICONDATAW make_icon(HWND owner, HICON icon) {
    NOTIFYICONDATAW data{};
    data.cbSize = sizeof(data);
    data.hWnd = owner;
    data.uID = kIconId;
    data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
    data.uCallbackMessage = kTrayCallbackMessage;
    data.hIcon = icon;
    const std::wstring tip = L"watashi overlay";
    std::copy(tip.begin(), tip.end(), data.szTip);
    return data;
}

HWND create_menu_owner() {
    if (!ensure_class()) {
        return nullptr;
    }
    // A normal, activatable top-level window: no WS_EX_NOACTIVATE, no
    // WS_EX_TRANSPARENT. It is parked offscreen and shown without activation so
    // SetForegroundWindow can make it foreground when the menu opens. Its
    // position is deliberately unchanged from the bisect.
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

std::string hex_pointer(const void* pointer) {
    static const char digits[] = "0123456789abcdef";
    std::uintptr_t value = reinterpret_cast<std::uintptr_t>(pointer);
    std::string text;
    do {
        text.push_back(digits[value & 0xf]);
        value >>= 4;
    } while (value != 0);
    std::reverse(text.begin(), text.end());
    return "0x" + text;
}

}  // namespace

TrayIcon::~TrayIcon() {
    remove();
}

bool TrayIcon::add(HWND icon_owner) {
    if (added_) {
        return true;
    }
    if (icon_owner == nullptr) {
        return false;
    }
    icon_owner_ = icon_owner;
    if (menu_owner_ == nullptr) {
        menu_owner_ = create_menu_owner();
    }
    if (menu_owner_ == nullptr) {
        last_error_ = GetLastError();
        return false;
    }
    if (icon_ == nullptr) {
        icon_ = LoadIconW(nullptr, MAKEINTRESOURCEW(32512));  // IDI_APPLICATION
    }

    // The icon belongs to the window that already exists (the overlay). A
    // separate 1x1 owner made the shell refuse NIM_ADD with error 5 on the
    // bisect machine; the overlay owner is the path that was accepted.
    NOTIFYICONDATAW data = make_icon(icon_owner_, icon_);
    SetLastError(0);
    added_ = Shell_NotifyIconW(NIM_ADD, &data) != FALSE;
    last_error_ = added_ ? 0 : GetLastError();
    if (!added_) {
        std::cerr << "tray: NIM_ADD failed owner=" << hex_pointer(icon_owner_)
                  << " hIcon=" << hex_pointer(icon_) << " error=" << last_error_
                  << "\n";
        return false;
    }

    // Version 4 puts the event in LOWORD(lParam) and the icon id in HIWORD.
    // The message loop reads LOWORD, which is also correct for the older
    // convention where lParam is the event itself.
    SetLastError(0);
    NOTIFYICONDATAW version = data;
    version.uVersion = NOTIFYICON_VERSION_4;
    if (Shell_NotifyIconW(NIM_SETVERSION, &version)) {
        std::cerr << "tray: version " << NOTIFYICON_VERSION_4 << " enabled\n";
    } else {
        std::cerr << "tray: NIM_SETVERSION(" << NOTIFYICON_VERSION_4
                  << ") failed error " << GetLastError() << "\n";
    }
    return true;
}

void TrayIcon::remove() {
    if (added_ && icon_owner_ != nullptr) {
        NOTIFYICONDATAW data = make_icon(icon_owner_, icon_);
        Shell_NotifyIconW(NIM_DELETE, &data);
        added_ = false;
    }
    if (menu_owner_ != nullptr) {
        DestroyWindow(menu_owner_);
        menu_owner_ = nullptr;
    }
    icon_owner_ = nullptr;
}

int TrayIcon::show_menu(bool paused, bool visible) {
    if (!added_ || menu_owner_ == nullptr) {
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
    // foreground. The owner is the menu window on purpose: SetForegroundWindow
    // on the NOACTIVATE overlay always fails.
    SetForegroundWindow(menu_owner_);
    const int chosen = TrackPopupMenu(
        menu,
        TPM_RETURNCMD | TPM_RIGHTBUTTON,
        cursor.x,
        cursor.y,
        0,
        menu_owner_,
        nullptr);
    DestroyMenu(menu);
    PostMessageW(menu_owner_, WM_NULL, 0, 0);
    return chosen;
}

}  // namespace watashi::ui
