// The notification-area icon and its menu.
//
// The menu is built from ``tray_menu`` so its ids and labels are testable
// without a shell; this file owns a small hidden owner window and wraps
// Shell_NotifyIcon and TrackPopupMenu.
//
// The owner is deliberately a *different* window from the overlay. The overlay
// carries WS_EX_NOACTIVATE, so SetForegroundWindow on it fails and the popup
// menu never opens (Windows only dismisses a popup reliably while its owner is
// foreground). The tray owner is a normal, activatable 1x1 window parked
// offscreen with WS_EX_TOOLWINDOW: it never appears in the taskbar or Alt-Tab
// and is not shown until the shell needs it. The overlay keeps every style it
// had.
//
// If the shell refuses the icon (a session without a notification area),
// ``add`` returns false and the overlay keeps running without a tray rather
// than failing to start.

#pragma once

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

namespace watashi::ui {

/// Owner-window callback message for mouse events on the icon.
inline constexpr UINT kTrayCallbackMessage = WM_APP + 10;

/// Class and title of the hidden owner. Public so tests can prove it is a
/// different HWND from the overlay.
inline constexpr wchar_t kTrayWindowClass[] = L"WatashiTrayOwner";
inline constexpr wchar_t kTrayWindowTitle[] = L"watashi tray";

class TrayIcon {
public:
    TrayIcon() = default;
    ~TrayIcon();

    TrayIcon(const TrayIcon&) = delete;
    TrayIcon& operator=(const TrayIcon&) = delete;

    /// Creates the owner window and adds the icon. False when either step
    /// failed; ``hwnd()`` still reports the owner when only the shell refused.
    bool add();
    void remove();

    [[nodiscard]] bool present() const noexcept { return added_; }
    /// The Win32 error from the last refused NIM_ADD; 0 when the shell accepted
    /// the icon or never set an error. For the operator, not for control flow.
    [[nodiscard]] DWORD last_error() const noexcept { return last_error_; }
    /// The tray owner window, distinct from the overlay. Null before ``add``.
    [[nodiscard]] HWND hwnd() const noexcept { return owner_; }

    /// Builds the menu from the current state, shows it at the cursor and
    /// returns the chosen command id, or 0 when dismissed.
    int show_menu(bool paused, bool visible);

private:
    HWND owner_ = nullptr;
    bool added_ = false;
    DWORD last_error_ = 0;
};

}  // namespace watashi::ui
