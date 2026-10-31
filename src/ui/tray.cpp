#include "watashi/ui/tray.hpp"

#include <shellapi.h>

#include <algorithm>
#include <string>
#include <vector>

#include "watashi/ui/actions.hpp"

namespace watashi::ui {
namespace {

constexpr UINT kIconId = 1;

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

}  // namespace

TrayIcon::~TrayIcon() {
    remove();
}

bool TrayIcon::add(HWND owner) {
    if (added_) {
        return true;
    }
    if (owner == nullptr) {
        return false;
    }
    owner_ = owner;
    NOTIFYICONDATAW data = make_icon(owner_);
    added_ = Shell_NotifyIconW(NIM_ADD, &data) != FALSE;
    return added_;
}

void TrayIcon::remove() {
    if (!added_ || owner_ == nullptr) {
        return;
    }
    NOTIFYICONDATAW data = make_icon(owner_);
    Shell_NotifyIconW(NIM_DELETE, &data);
    added_ = false;
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
    // The menu has to be dismissed by clicking elsewhere; Windows only does
    // that reliably when the owner is foreground while the menu is up.
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
