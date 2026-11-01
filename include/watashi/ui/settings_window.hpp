// The native settings window. Every control is generated from
// config/settings_ui.json; this file adds no field of its own.
//
// It is a separate top-level window, not a dialog over the overlay: closing it
// never closes the subtitle window. Save validates every control in place,
// marks invalid ones red, persists config/user.json atomically, and only then
// sends the stdin commands the engine implements. Fields whose scope is
// restart are labelled restart and are never sent as live commands.

#pragma once

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <functional>
#include <memory>
#include <string>

#include <nlohmann/json.hpp>

#include "watashi/ui/settings.hpp"

namespace watashi::ui {

struct SettingsWindowCallbacks {
    /// Send one JSON command to the engine. False when it is not running.
    std::function<bool(const nlohmann::json&)> send_command;
    /// Keep the overlay's own target-language state in step after a live save.
    std::function<void(const std::string&)> target_language_changed;
    /// The Reload corpus button. False when the engine is not running.
    std::function<bool()> reload_corpus;
};

class SettingsWindow {
public:
    SettingsWindow(SettingsStore& store, SettingsWindowCallbacks callbacks);
    ~SettingsWindow();

    SettingsWindow(const SettingsWindow&) = delete;
    SettingsWindow& operator=(const SettingsWindow&) = delete;

    /// Creates and shows the window. False when there is no desktop session.
    bool create();
    void destroy();

    [[nodiscard]] HWND hwnd() const noexcept;
    [[nodiscard]] bool open() const noexcept;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace watashi::ui
