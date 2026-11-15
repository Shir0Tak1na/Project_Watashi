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
    auto* self = reinterpret_cast<TrayIcon*>(
        GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    if (self != nullptr && message == kTrayCallbackMessage) {
        self->forward_callback(wparam, lparam);
        return 0;
    }
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
    // NIF_SHOWTIP is only valid after NIM_SETVERSION(4). Putting it on
    // NIM_ADD makes the shell reject the add with error 5 on Windows 11, so
    // the initial add uses the classic flags and the tooltip is enabled with
    // NIM_MODIFY after the version is set.
    data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
    data.uCallbackMessage = kTrayCallbackMessage;
    data.hIcon = icon;
    const std::wstring tip = L"watashi overlay";
    std::copy(tip.begin(), tip.end(), data.szTip);
    return data;
}

HWND create_menu_owner(TrayIcon* self) {
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
        SetWindowLongPtrW(owner, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(self));
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

TrayEventAction tray_event_action(UINT event) noexcept {
    switch (event) {
        case NIN_SELECT:
        case NIN_KEYSELECT:
        case WM_LBUTTONUP:
            return TrayEventAction::kPrimary;
        case WM_CONTEXTMENU:
        case WM_RBUTTONUP:
            return TrayEventAction::kContext;
        default:
            return TrayEventAction::kNone;
    }
}

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
    overlay_owner_ = icon_owner;
    owner_name_ = "none";
    if (menu_owner_ == nullptr) {
        menu_owner_ = create_menu_owner(this);
    }
    if (menu_owner_ == nullptr) {
        last_error_ = GetLastError();
        return false;
    }
    if (icon_ == nullptr) {
        icon_ = LoadIconW(nullptr, MAKEINTRESOURCEW(32512));  // IDI_APPLICATION
    }

    // Prefer the normal hidden menu owner for the icon: a dedicated top-level
    // window is the reliable path for callbacks and tooltips. The layered
    // no-activate overlay is the fallback because one shell historically
    // refused the separate owner with error 5.
    auto try_add = [&](HWND owner, const char* name) -> bool {
        NOTIFYICONDATAW data = make_icon(owner, icon_);
        SetLastError(0);
        if (Shell_NotifyIconW(NIM_ADD, &data)) {
            icon_owner_ = owner;
            owner_name_ = name;
            added_ = true;
            last_error_ = 0;
            std::cerr << "tray: icon added owner=" << name << " hwnd="
                      << hex_pointer(owner) << "\n";
            return true;
        }
        last_error_ = GetLastError();
        std::cerr << "tray: NIM_ADD failed owner=" << name << " hwnd="
                  << hex_pointer(owner) << " hIcon=" << hex_pointer(icon_)
                  << " error=" << last_error_ << "\n";
        return false;
    };

    // A normal top-level owner is the reliable path for callbacks and
    // tooltips. The layered no-activate overlay is kept as a fallback because
    // some shells historically refused the dedicated owner with error 5.
    wchar_t force_overlay[8]{};
    const bool overlay_only =
        GetEnvironmentVariableW(L"WATASHI_TRAY_OVERLAY", force_overlay, 8) != 0 &&
        force_overlay[0] == L'1';
    bool chose = false;
    if (!overlay_only && menu_owner_ != nullptr) {
        chose = try_add(menu_owner_, "menu-owner");
    }
    if (!chose) {
        chose = try_add(icon_owner_, "overlay");
    }
    if (!chose && !overlay_only && menu_owner_ != nullptr) {
        chose = try_add(menu_owner_, "menu-owner");
    }
    if (!chose) {
        return false;
    }

    // Version 4 puts the event in LOWORD(lParam) and the icon id in HIWORD.
    // The message loop reads LOWORD, which is also correct for the older
    // convention where lParam is the event itself.
    SetLastError(0);
    NOTIFYICONDATAW version = make_icon(icon_owner_, icon_);
    version.uVersion = NOTIFYICON_VERSION_4;
    if (Shell_NotifyIconW(NIM_SETVERSION, &version)) {
        version4_ = true;
        std::cerr << "tray: version " << NOTIFYICON_VERSION_4 << " enabled\n";
        // Enable the standard tooltip only after version 4 is active.
        NOTIFYICONDATAW showtip = make_icon(icon_owner_, icon_);
        showtip.uFlags = NIF_SHOWTIP | NIF_TIP;
        if (!Shell_NotifyIconW(NIM_MODIFY, &showtip)) {
            std::cerr << "tray: NIF_SHOWTIP enable failed error "
                      << GetLastError() << "\n";
        } else {
            std::cerr << "tray: NIF_SHOWTIP enabled\n";
        }
    } else {
        // The shell keeps the default (legacy) event convention. The callback
        // mapper handles both WM_* and NIN_* values, so no re-add is needed.
        version4_ = false;
        std::cerr << "tray: NIM_SETVERSION(" << NOTIFYICON_VERSION_4
                  << ") failed error " << GetLastError()
                  << "; falling back to legacy tray events\n";
    }
    return true;
}

void TrayIcon::remove() {
    if (added_ && icon_owner_ != nullptr) {
        NOTIFYICONDATAW data = make_icon(icon_owner_, icon_);
        Shell_NotifyIconW(NIM_DELETE, &data);
        added_ = false;
        owner_name_ = "none";
    }
    if (menu_owner_ != nullptr) {
        DestroyWindow(menu_owner_);
        menu_owner_ = nullptr;
    }
    icon_owner_ = nullptr;
    owner_name_ = "none";
}

void TrayIcon::forward_callback(WPARAM wparam, LPARAM lparam) {
    if (overlay_owner_ != nullptr) {
        PostMessageW(overlay_owner_, kTrayCallbackMessage, wparam, lparam);
    }
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
    SetLastError(0);
    const BOOL foreground = SetForegroundWindow(menu_owner_);
    SetLastError(0);
    const int chosen = TrackPopupMenu(
        menu,
        TPM_RETURNCMD | TPM_RIGHTBUTTON,
        cursor.x,
        cursor.y,
        0,
        menu_owner_,
        nullptr);
    const DWORD menu_error = GetLastError();
    std::cerr << "tray: menu owner=" << hex_pointer(menu_owner_)
              << " foreground=" << (foreground ? 1 : 0)
              << " chosen=" << chosen << " error=" << menu_error << "\n";
    DestroyMenu(menu);
    PostMessageW(menu_owner_, WM_NULL, 0, 0);
    return chosen;
}

}  // namespace watashi::ui
