// Pure hotkey and tray tables, plus the command payloads they send.
//
// Registering a hotkey or painting a tray menu needs USER32, but the ids, the
// key chords, the menu labels and the JSON sent to the engine do not. Keeping
// those here means the headless tests pin the contract and the hotkey path and
// the tray path cannot drift apart.

#pragma once

#include <array>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json.hpp>

namespace watashi::ui {

inline constexpr int kHotkeyPause = 1;
inline constexpr int kHotkeyVisibility = 2;
inline constexpr int kHotkeyLanguage = 3;

/// MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, as unsigned so this header stays free
/// of windows.h for headless tests.
inline constexpr unsigned kHotkeyModifiers = 0x0001u | 0x0002u | 0x4000u;

struct HotkeyBinding {
    int id = 0;
    unsigned modifiers = 0;
    unsigned virtual_key = 0;
};

/// Ctrl+Alt+P / H / T. Virtual-key codes are the ASCII values of the letters.
[[nodiscard]] std::array<HotkeyBinding, 3> hotkey_bindings();

inline constexpr int kTrayCommandPause = 1001;
inline constexpr int kTrayCommandVisibility = 1002;
inline constexpr int kTrayCommandExit = 1003;

struct TrayMenuEntry {
    int id = 0;
    std::string label;
    bool checked = false;
};

/// What a chosen menu id does. Kept separate from the menu labels so the
/// mapping can be asserted without a shell or a window.
enum class TrayAction {
    kNone,
    kTogglePause,
    kToggleVisibility,
    kExit,
};

/// Menu contents for the current state.
[[nodiscard]] std::vector<TrayMenuEntry> tray_menu(bool paused, bool visible);

/// The action behind a menu id. Unknown ids answer ``kNone``.
[[nodiscard]] TrayAction tray_action(int id) noexcept;

[[nodiscard]] nlohmann::json pause_command(bool should_pause);
[[nodiscard]] nlohmann::json target_language_command(std::string_view language);
[[nodiscard]] nlohmann::json shutdown_command();

/// Ctrl+Alt+T alternates the two languages the acceptance cares about.
[[nodiscard]] std::string next_target_language(std::string_view current);

}  // namespace watashi::ui
