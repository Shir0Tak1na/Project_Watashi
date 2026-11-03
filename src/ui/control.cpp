#include "watashi/ui/control.hpp"

#include "watashi/ui/actions.hpp"

namespace watashi::ui {

ControlAction control_action_from_id(int id) noexcept {
    switch (id) {
        case kTrayCommandPause:
        case kHotkeyPause:
            return ControlAction::kTogglePause;
        case kTrayCommandVisibility:
        case kHotkeyVisibility:
            return ControlAction::kToggleVisibility;
        case kTrayCommandExit:
        case kHotkeyQuit:
            return ControlAction::kExit;
        default:
            return ControlAction::kNone;
    }
}

bool parse_control_surface(std::string_view text,
                           ControlSurface& surface) noexcept {
    if (text == "auto") {
        surface = ControlSurface::kAuto;
        return true;
    }
    if (text == "tray") {
        surface = ControlSurface::kTray;
        return true;
    }
    if (text == "window") {
        surface = ControlSurface::kWindow;
        return true;
    }
    if (text == "none") {
        surface = ControlSurface::kNone;
        return true;
    }
    return false;
}

const char* control_surface_name(bool tray_added, bool window_created) noexcept {
    if (tray_added) {
        return "tray";
    }
    if (window_created) {
        return "window";
    }
    return "hotkeys";
}

}  // namespace watashi::ui
