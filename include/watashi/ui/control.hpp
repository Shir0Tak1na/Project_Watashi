// One action vocabulary for the tray, the control window and the hotkeys.

#pragma once

#include <string_view>

namespace watashi::ui {

enum class ControlAction {
    kNone,
    kTogglePause,
    kToggleVisibility,
    kExit,
};

/// Map a menu id, button id or hotkey id to the one action it performs. Unknown
/// ids answer ``kNone`` so every surface shares the same table.
[[nodiscard]] ControlAction control_action_from_id(int id) noexcept;

/// The startup policy for the control surface: try the tray first (auto), tray
/// only, force the control window, or hotkeys only.
enum class ControlSurface {
    kAuto,
    kTray,
    kWindow,
    kNone,
};

/// Parse the ``--control`` value. Returns false for anything but the four
/// documented spellings.
[[nodiscard]] bool parse_control_surface(std::string_view text,
                                         ControlSurface& surface) noexcept;

/// "tray", "window" or "hotkeys": what the report records after startup.
[[nodiscard]] const char* control_surface_name(bool tray_added,
                                               bool window_created) noexcept;

}  // namespace watashi::ui
