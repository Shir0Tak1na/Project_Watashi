// Settings described by config/settings_ui.json.
//
// The window renders whatever the generated document contains; no field is
// hard-coded here. This module owns the parts a headless test can check:
// parsing that document, validating a form value, merging config/user.json,
// writing only non-default values atomically, and turning a live field into
// the stdin command the engine already understands.
//
// Scope: the schema says whether a field is live or restart in principle. The
// C++ engine only implements live commands for target language, scene, fps and
// diff threshold; every other field is treated as restart rather than
// pretending that saving it changed the running engine.

#pragma once

#include <cstddef>
#include <filesystem>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json.hpp>

namespace watashi::ui {

struct SettingsField {
    std::string key;
    std::string label;
    /// One of: bool, int, float, enum, string, path.
    std::string type;
    /// The Python kind, kept so path_list and region survive the type mapping.
    std::string kind;
    std::string description;
    /// The schema's applies value: "live" or "restart".
    std::string scope;
    std::string unit;
    std::string note;
    std::string cost;
    std::string danger;
    std::string command;

    nlohmann::json default_value = nullptr;
    nlohmann::json value = nullptr;
    std::vector<std::string> choices;
    bool has_low = false;
    bool has_high = false;
    double low = 0.0;
    double high = 0.0;

    [[nodiscard]] bool is_list() const noexcept { return kind == "path_list"; }
    [[nodiscard]] bool is_region() const noexcept { return kind == "region"; }
    [[nodiscard]] bool is_numeric() const noexcept {
        return type == "int" || type == "float";
    }
};

struct SettingsGroup {
    std::string id;
    std::string title;
    std::string summary;
    std::vector<SettingsField> fields;
};

/// Parsed config/settings_ui.json.
class SettingsSchema {
public:
    SettingsSchema() = default;

    /// Reads and validates the generated document. False with ``error`` set
    /// when the file is missing, malformed, or a required member is absent.
    static SettingsSchema load(const std::filesystem::path& path, std::string* error);
    static SettingsSchema from_json(const nlohmann::json& document, std::string* error);

    [[nodiscard]] const std::vector<SettingsGroup>& groups() const noexcept {
        return groups_;
    }
    /// The store owns its schema by value and needs to set current values.
    [[nodiscard]] std::vector<SettingsGroup>& groups_mutable() noexcept {
        return groups_;
    }
    [[nodiscard]] std::size_t field_count() const noexcept;
    [[nodiscard]] const SettingsField* find(std::string_view key) const noexcept;
    [[nodiscard]] SettingsField* find_mutable(std::string_view key) noexcept;
    [[nodiscard]] std::vector<std::string> keys() const;
    [[nodiscard]] bool empty() const noexcept { return groups_.empty(); }

private:
    std::vector<SettingsGroup> groups_;
};

/// Validate one value for a field and return the normalized JSON. ``error``
/// explains a false result; it is suitable for the status line.
[[nodiscard]] bool validate_setting(
    const SettingsField& field,
    const nlohmann::json& raw,
    nlohmann::json& normalized,
    std::string* error);

/// True for the keys whose changes the C++ engine can apply through stdin.
[[nodiscard]] bool is_live_key(std::string_view key) noexcept;

/// "live" only when the field says live *and* the engine has a command for it;
/// everything else is "restart" so the window never promises a hot change it
/// cannot deliver.
[[nodiscard]] std::string effective_scope(const SettingsField& field);

/// The stdin command for a live key, or a null JSON when there is none.
/// The diff threshold is converted from the schema's 0-255/percent scale to
/// the engine's 0-1 fraction (2.0 in user.json means 0.02).
[[nodiscard]] nlohmann::json live_command(
    const SettingsField& field,
    const nlohmann::json& value);

[[nodiscard]] nlohmann::json reload_corpus_command();

/// The current values, merged from defaults and config/user.json.
///
/// The file is a flat object keyed by the schema's dotted keys, for example:
///
///   {"translation.target": "en", "capture.fps": 5}
///
/// Flat keys make unknown-key detection exact; nested sections would need a
/// merge policy of their own. Only values that differ from the schema default
/// are written back, so a file that is deleted or emptied reproduces the
/// defaults.
class SettingsStore {
public:
    SettingsStore(SettingsSchema schema, std::filesystem::path config_path);

    /// Merges the file into the defaults. A missing file is not an error.
    /// Unknown keys and invalid values are reported in ``warnings`` and do not
    /// change the current value.
    bool load(std::vector<std::string>* warnings, std::string* error);

    [[nodiscard]] const SettingsSchema& schema() const noexcept { return schema_; }
    [[nodiscard]] const std::filesystem::path& path() const noexcept { return path_; }
    [[nodiscard]] nlohmann::json value(std::string_view key) const;
    [[nodiscard]] const nlohmann::json* find_value(std::string_view key) const;

    /// Case for tests and the save path: an unknown key is refused.
    bool set_value(std::string_view key, const nlohmann::json& value, std::string* error);

    void reset_defaults();
    [[nodiscard]] bool is_default(std::string_view key) const;
    [[nodiscard]] std::vector<std::string> changed_keys() const;

    /// The flat object that would be written: non-default values only.
    [[nodiscard]] nlohmann::json persisted_document() const;

    /// Writes the persisted document atomically (temp file then replace).
    bool save(std::string* error);

    /// Render one value for an edit control (path_list joined by commas).
    [[nodiscard]] static std::string display_text(const SettingsField& field,
                                                  const nlohmann::json& value);
    /// Parse an edit control back into JSON; false with ``error`` on bad input.
    [[nodiscard]] static bool parse_text(const SettingsField& field,
                                         const std::string& text,
                                         nlohmann::json& value,
                                         std::string* error);

private:
    SettingsSchema schema_;
    std::filesystem::path path_;
};

}  // namespace watashi::ui
