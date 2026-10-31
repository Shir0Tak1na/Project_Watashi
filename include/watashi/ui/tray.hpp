// The notification-area icon and its menu.
//
// The menu is built from ``tray_menu`` so its ids and labels are testable
// without a shell; this file only wraps Shell_NotifyIcon and TrackPopupMenu.
// If the shell refuses the icon (a headless or locked-down session), ``add``
// returns false and the overlay keeps running without a tray rather than
// failing to start.

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

class TrayIcon {
public:
    TrayIcon() = default;
    ~TrayIcon();

    TrayIcon(const TrayIcon&) = delete;
    TrayIcon& operator=(const TrayIcon&) = delete;

    bool add(HWND owner);
    void remove();
    [[nodiscard]] bool present() const noexcept { return added_; }

    /// Builds the menu from the current state, shows it at the cursor and
    /// returns the chosen command id, or 0 when dismissed.
    int show_menu(bool paused, bool visible);

private:
    HWND owner_ = nullptr;
    bool added_ = false;
};

}  // namespace watashi::ui
