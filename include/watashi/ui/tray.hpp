// The notification-area icon and its menu.
//
// Two owners, because the two jobs need different window styles:
//
//   icon_owner  the Shell_NotifyIcon hWnd. It is the overlay itself: the shell
//               accepts the icon from the window that already exists, and the
//               icon's callback messages arrive on that window.
//   menu_owner  a normal, activatable 1x1 WS_EX_TOOLWINDOW parked offscreen.
//               Only SetForegroundWindow and TrackPopupMenu use it. The overlay
//               carries WS_EX_NOACTIVATE, so SetForegroundWindow on it always
//               fails and the popup would never dismiss.
//
// The overlay keeps every style it had. If the shell refuses the icon, ``add``
// returns false and the overlay keeps running without a tray.

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

/// What a raw tray callback event means. NOTIFYICON_VERSION_4 reports the
/// event in LOWORD(lParam); legacy reports it as the whole lParam. Keeping the
/// mapping pure makes the click behaviour testable without a shell.
enum class TrayEventAction {
    kNone,
    kPrimary,  // NIN_SELECT / NIN_KEYSELECT / WM_LBUTTONUP
    kContext,  // WM_CONTEXTMENU / WM_RBUTTONUP
};

TrayEventAction tray_event_action(UINT event) noexcept;

/// Class and title of the menu owner. Public so tests can prove it is a
/// different HWND from the overlay.
inline constexpr wchar_t kTrayWindowClass[] = L"WatashiTrayOwner";
inline constexpr wchar_t kTrayWindowTitle[] = L"watashi tray";

class TrayIcon {
public:
    TrayIcon() = default;
    ~TrayIcon();

    TrayIcon(const TrayIcon&) = delete;
    TrayIcon& operator=(const TrayIcon&) = delete;

    /// Adds the icon owned by ``icon_owner`` (the overlay) and creates the
    /// separate menu owner. False when the shell refused the icon or the menu
    /// owner could not be created; ``last_error`` carries the Win32 code.
    bool add(HWND icon_owner);
    void remove();

    [[nodiscard]] bool present() const noexcept { return added_; }
    [[nodiscard]] DWORD last_error() const noexcept { return last_error_; }
    /// The window the icon and its callback messages belong to.
    [[nodiscard]] HWND icon_hwnd() const noexcept { return icon_owner_; }
    /// The window that owns the popup menu; always different from icon_hwnd().
    [[nodiscard]] HWND menu_hwnd() const noexcept { return menu_owner_; }
    /// Diagnostic: which window the shell accepted for the icon.
    [[nodiscard]] const char* owner_name() const noexcept { return owner_name_; }

    /// Builds the menu from the current state, shows it at the cursor and
    /// returns the chosen command id, or 0 when dismissed.
    int show_menu(bool paused, bool visible);

    /// Called by the fallback owner's window procedure. It forwards the raw
    /// tray callback to the overlay so the main loop can handle it.
    void forward_callback(WPARAM wparam, LPARAM lparam);

private:
    HWND icon_owner_ = nullptr;
    HWND menu_owner_ = nullptr;
    HWND overlay_owner_ = nullptr;
    HICON icon_ = nullptr;
    bool added_ = false;
    bool version4_ = false;
    DWORD last_error_ = 0;
    const char* owner_name_ = "none";
};

}  // namespace watashi::ui
