#include "watashi/ui/actions.hpp"

#include <array>
#include <string>
#include <string_view>
#include <vector>

namespace watashi::ui {

std::array<HotkeyBinding, 3> hotkey_bindings() {
    return std::array<HotkeyBinding, 3>{{
        {kHotkeyPause, kHotkeyModifiers, 'P'},
        {kHotkeyVisibility, kHotkeyModifiers, 'H'},
        {kHotkeyLanguage, kHotkeyModifiers, 'T'},
    }};
}

std::vector<TrayMenuEntry> tray_menu(bool paused, bool visible) {
    return {
        TrayMenuEntry{kTrayCommandPause, paused ? "Continue" : "Pause", paused},
        TrayMenuEntry{kTrayCommandVisibility, visible ? "Hide" : "Show", !visible},
        TrayMenuEntry{kTrayCommandExit, "Exit", false},
    };
}

TrayAction tray_action(int id) noexcept {
    switch (id) {
        case kTrayCommandPause:
            return TrayAction::kTogglePause;
        case kTrayCommandVisibility:
            return TrayAction::kToggleVisibility;
        case kTrayCommandExit:
            return TrayAction::kExit;
        default:
            return TrayAction::kNone;
    }
}

nlohmann::json pause_command(bool should_pause) {
    return nlohmann::json{
        {"cmd", should_pause ? "pause" : "resume"},
    };
}

nlohmann::json target_language_command(std::string_view language) {
    return nlohmann::json{
        {"cmd", "set_target_lang"},
        {"target_lang", std::string(language)},
    };
}

nlohmann::json shutdown_command() {
    return nlohmann::json{{"cmd", "shutdown"}};
}

std::string next_target_language(std::string_view current) {
    return current == "en" ? "zh-CN" : "en";
}

}  // namespace watashi::ui
