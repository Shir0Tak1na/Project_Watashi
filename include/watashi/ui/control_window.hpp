// A small always-available control surface for hosts where the notification
// area refuses the icon. It is a normal top-level window: clickable, closable,
// and never a substitute for the tray on machines where the tray works.

#pragma once

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <functional>

#include "watashi/ui/control.hpp"

namespace watashi::ui {

class ControlWindow {
public:
    using ActionHandler = std::function<void(ControlAction)>;

    ControlWindow() = default;
    ~ControlWindow();

    ControlWindow(const ControlWindow&) = delete;
    ControlWindow& operator=(const ControlWindow&) = delete;

    /// Creates the window and its three buttons. It is not shown until show().
    bool create(HINSTANCE instance, ActionHandler handler);
    void show();
    void hide();
    void toggle();
    [[nodiscard]] bool visible() const;
    [[nodiscard]] HWND hwnd() const noexcept { return hwnd_; }

    /// Re-label Pause/Resume and Hide/Show from the current state.
    void update(bool paused, bool overlay_visible);

    // Public because the class registration must point at it.
    static LRESULT CALLBACK window_proc(HWND hwnd, UINT message,
                                       WPARAM wparam, LPARAM lparam);

private:
    LRESULT handle_message(UINT message, WPARAM wparam, LPARAM lparam);

    HWND hwnd_ = nullptr;
    HWND pause_button_ = nullptr;
    HWND visibility_button_ = nullptr;
    HWND exit_button_ = nullptr;
    ActionHandler handler_;
    bool visible_ = false;
};

}  // namespace watashi::ui
