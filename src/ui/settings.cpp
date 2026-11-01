#include "watashi/ui/settings.hpp"

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <cmath>
#include <cstdlib>
#include <fstream>
#include <set>
#include <sstream>
#include <system_error>
#include <utility>

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#endif

namespace watashi::ui {
namespace {

std::string trim(const std::string& text) {
    std::size_t begin = 0;
    std::size_t end = text.size();
    while (begin < end) {
        const char c = text[begin];
        if (c != ' ' && c != '\t' && c != '\r' && c != '\n') {
            break;
        }
        ++begin;
    }
    while (end > begin) {
        const char c = text[end - 1];
        if (c != ' ' && c != '\t' && c != '\r' && c != '\n') {
            break;
        }
        --end;
    }
    return text.substr(begin, end - begin);
}

bool require_member(
    const nlohmann::json& object,
    const char* name,
    nlohmann::json::value_t type,
    std::string* error,
    std::string* out) {
    const auto found = object.find(name);
    if (found == object.end()) {
        if (error != nullptr) {
            *error = std::string("missing \"") + name + "\"";
        }
        return false;
    }
    if (found->type() != type) {
        if (error != nullptr) {
            *error = std::string("\"") + name + "\" has the wrong type";
        }
        return false;
    }
    if (out != nullptr && type == nlohmann::json::value_t::string) {
        *out = found->get<std::string>();
    }
    return true;
}

bool in_choices(const std::string& value, const std::vector<std::string>& choices) {
    return std::find(choices.begin(), choices.end(), value) != choices.end();
}

bool check_range(const SettingsField& field, double value, std::string* error) {
    if (field.has_low && value < field.low) {
        if (error != nullptr) {
            *error = "must be >= " + std::to_string(field.low) + field.unit;
        }
        return false;
    }
    if (field.has_high && value > field.high) {
        if (error != nullptr) {
            *error = "must be <= " + std::to_string(field.high) + field.unit;
        }
        return false;
    }
    return true;
}

bool parse_region(const std::string& text, std::string* error) {
    std::stringstream stream(text);
    int values[4]{};
    char comma = 0;
    for (int index = 0; index < 4; ++index) {
        if (!(stream >> values[index])) {
            if (error != nullptr) {
                *error = "expected x,y,w,h";
            }
            return false;
        }
        if (index < 3) {
            if (!(stream >> comma) || comma != ',') {
                if (error != nullptr) {
                    *error = "expected x,y,w,h";
                }
                return false;
            }
        }
    }
    if (!(stream >> std::ws).eof()) {
        if (error != nullptr) {
            *error = "expected x,y,w,h";
        }
        return false;
    }
    if (values[2] <= 0 || values[3] <= 0) {
        if (error != nullptr) {
            *error = "width and height must be positive";
        }
        return false;
    }
    return true;
}

nlohmann::json split_path_list(const std::string& text) {
    nlohmann::json array = nlohmann::json::array();
    std::string current;
    for (const char c : text) {
        if (c == ',') {
            const std::string part = trim(current);
            if (!part.empty()) {
                array.push_back(part);
            }
            current.clear();
        } else {
            current.push_back(c);
        }
    }
    const std::string part = trim(current);
    if (!part.empty()) {
        array.push_back(part);
    }
    return array;
}

}  // namespace

SettingsSchema SettingsSchema::from_json(
    const nlohmann::json& document,
    std::string* error) {
    SettingsSchema schema;
    if (!document.is_object()) {
        if (error != nullptr) {
            *error = "settings document must be an object";
        }
        return schema;
    }
    const auto groups = document.find("groups");
    if (groups == document.end() || !groups->is_array()) {
        if (error != nullptr) {
            *error = "settings document has no groups array";
        }
        return schema;
    }

    std::set<std::string> seen_keys;
    for (const auto& group_json : *groups) {
        if (!group_json.is_object()) {
            if (error != nullptr) {
                *error = "a group is not an object";
            }
            return {};
        }
        SettingsGroup group;
        std::string group_error;
        if (!require_member(group_json, "id", nlohmann::json::value_t::string,
                            &group_error, &group.id) ||
            !require_member(group_json, "title", nlohmann::json::value_t::string,
                            &group_error, &group.title)) {
            if (error != nullptr) {
                *error = "group: " + group_error;
            }
            return {};
        }
        if (const auto summary = group_json.find("summary");
            summary != group_json.end() && summary->is_string()) {
            group.summary = summary->get<std::string>();
        }

        const auto fields = group_json.find("fields");
        if (fields == group_json.end() || !fields->is_array()) {
            if (error != nullptr) {
                *error = "group " + group.id + " has no fields array";
            }
            return {};
        }
        for (const auto& field_json : *fields) {
            if (!field_json.is_object()) {
                if (error != nullptr) {
                    *error = "a field is not an object";
                }
                return {};
            }
            SettingsField field;
            std::string field_error;
            if (!require_member(field_json, "key", nlohmann::json::value_t::string,
                                &field_error, &field.key) ||
                !require_member(field_json, "label", nlohmann::json::value_t::string,
                                &field_error, &field.label) ||
                !require_member(field_json, "type", nlohmann::json::value_t::string,
                                &field_error, &field.type) ||
                !require_member(field_json, "scope", nlohmann::json::value_t::string,
                                &field_error, &field.scope)) {
                if (error != nullptr) {
                    *error = "field: " + field_error;
                }
                return {};
            }
            const auto description = field_json.find("description");
            if (description != field_json.end() && description->is_string()) {
                field.description = description->get<std::string>();
            }
            const auto default_value = field_json.find("default");
            if (default_value == field_json.end()) {
                if (error != nullptr) {
                    *error = "field " + field.key + " has no default";
                }
                return {};
            }
            field.default_value = *default_value;
            field.value = field.default_value;

            if (const auto kind = field_json.find("kind");
                kind != field_json.end() && kind->is_string()) {
                field.kind = kind->get<std::string>();
            } else {
                field.kind = field.type;
            }
            if (const auto choices = field_json.find("choices");
                choices != field_json.end() && choices->is_array()) {
                for (const auto& choice : *choices) {
                    if (choice.is_string()) {
                        field.choices.push_back(choice.get<std::string>());
                    }
                }
            }
            if (const auto low = field_json.find("low");
                low != field_json.end() && low->is_number()) {
                field.has_low = true;
                field.low = low->get<double>();
            }
            if (const auto high = field_json.find("high");
                high != field_json.end() && high->is_number()) {
                field.has_high = true;
                field.high = high->get<double>();
            }
            for (const char* name : {"unit", "note", "cost", "danger", "command"}) {
                const auto found = field_json.find(name);
                if (found == field_json.end() || !found->is_string()) {
                    continue;
                }
                std::string* target = nullptr;
                if (std::string(name) == "unit") target = &field.unit;
                if (std::string(name) == "note") target = &field.note;
                if (std::string(name) == "cost") target = &field.cost;
                if (std::string(name) == "danger") target = &field.danger;
                if (std::string(name) == "command") target = &field.command;
                if (target != nullptr) {
                    *target = found->get<std::string>();
                }
            }

            if (field.type != "bool" && field.type != "int" && field.type != "float" &&
                field.type != "enum" && field.type != "string" && field.type != "path") {
                if (error != nullptr) {
                    *error = "field " + field.key + " has unknown type " + field.type;
                }
                return {};
            }
            if (field.scope != "live" && field.scope != "restart") {
                if (error != nullptr) {
                    *error = "field " + field.key + " has unknown scope " + field.scope;
                }
                return {};
            }
            if (field.type == "enum" && field.choices.empty()) {
                if (error != nullptr) {
                    *error = "enum field " + field.key + " has no choices";
                }
                return {};
            }
            if (!seen_keys.insert(field.key).second) {
                if (error != nullptr) {
                    *error = "duplicate field key " + field.key;
                }
                return {};
            }
            group.fields.push_back(std::move(field));
        }
        schema.groups_.push_back(std::move(group));
    }
    if (schema.groups_.empty()) {
        if (error != nullptr) {
            *error = "settings document has no groups";
        }
        return {};
    }
    return schema;
}

SettingsSchema SettingsSchema::load(const std::filesystem::path& path, std::string* error) {
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        if (error != nullptr) {
            *error = "cannot read " + path.string();
        }
        return {};
    }
    nlohmann::json document;
    try {
        file >> document;
    } catch (const nlohmann::json::exception& exception) {
        if (error != nullptr) {
            *error = path.string() + ": " + exception.what();
        }
        return {};
    }
    return from_json(document, error);
}

std::size_t SettingsSchema::field_count() const noexcept {
    std::size_t total = 0;
    for (const auto& group : groups_) {
        total += group.fields.size();
    }
    return total;
}

const SettingsField* SettingsSchema::find(std::string_view key) const noexcept {
    for (const auto& group : groups_) {
        for (const auto& field : group.fields) {
            if (field.key == key) {
                return &field;
            }
        }
    }
    return nullptr;
}

SettingsField* SettingsSchema::find_mutable(std::string_view key) noexcept {
    for (auto& group : groups_) {
        for (auto& field : group.fields) {
            if (field.key == key) {
                return &field;
            }
        }
    }
    return nullptr;
}

std::vector<std::string> SettingsSchema::keys() const {
    std::vector<std::string> result;
    result.reserve(field_count());
    for (const auto& group : groups_) {
        for (const auto& field : group.fields) {
            result.push_back(field.key);
        }
    }
    return result;
}

bool validate_setting(
    const SettingsField& field,
    const nlohmann::json& raw,
    nlohmann::json& normalized,
    std::string* error) {
    const std::string& kind = field.kind;
    if (kind == "bool") {
        if (!raw.is_boolean()) {
            if (error != nullptr) *error = "needs true or false";
            return false;
        }
        normalized = raw.get<bool>();
        return true;
    }

    if (kind == "int") {
        if (raw.is_null()) {
            if (field.default_value.is_null()) {
                normalized = nullptr;
                return true;
            }
            if (error != nullptr) *error = "needs an integer";
            return false;
        }
        long long value = 0;
        if (raw.is_number_integer()) {
            value = raw.get<long long>();
        } else if (raw.is_number()) {
            const double number = raw.get<double>();
            if (!std::isfinite(number) || std::floor(number) != number) {
                if (error != nullptr) *error = "needs a whole number";
                return false;
            }
            value = static_cast<long long>(number);
        } else {
            if (error != nullptr) *error = "needs an integer";
            return false;
        }
        if (!check_range(field, static_cast<double>(value), error)) {
            return false;
        }
        normalized = value;
        return true;
    }

    if (kind == "float") {
        if (raw.is_null()) {
            if (field.default_value.is_null()) {
                normalized = nullptr;
                return true;
            }
            if (error != nullptr) *error = "needs a number";
            return false;
        }
        if (!raw.is_number()) {
            if (error != nullptr) *error = "needs a number";
            return false;
        }
        const double value = raw.get<double>();
        if (!std::isfinite(value)) {
            if (error != nullptr) *error = "needs a finite number";
            return false;
        }
        if (!check_range(field, value, error)) {
            return false;
        }
        normalized = value;
        return true;
    }

    if (kind == "enum") {
        if (raw.is_null()) {
            if (field.default_value.is_null()) {
                normalized = nullptr;
                return true;
            }
            if (error != nullptr) *error = "needs one of the choices";
            return false;
        }
        if (!raw.is_string()) {
            if (error != nullptr) *error = "needs one of the choices";
            return false;
        }
        if (field.choices.empty()) {
            if (error != nullptr) *error = "this field declares no choices";
            return false;
        }
        const std::string value = raw.get<std::string>();
        if (!in_choices(value, field.choices)) {
            if (error != nullptr) {
                *error = "must be one of " + field.choices.front();
                for (std::size_t index = 1; index < field.choices.size(); ++index) {
                    *error += "/" + field.choices[index];
                }
            }
            return false;
        }
        normalized = value;
        return true;
    }

    if (kind == "path_list") {
        nlohmann::json array = nlohmann::json::array();
        if (raw.is_null()) {
            // An empty list is the meaningful "unset"; null collapses to it.
            normalized = array;
            return true;
        }
        if (raw.is_string()) {
            normalized = split_path_list(raw.get<std::string>());
            return true;
        }
        if (!raw.is_array()) {
            if (error != nullptr) *error = "needs a list of paths";
            return false;
        }
        for (const auto& entry : raw) {
            if (!entry.is_string()) {
                if (error != nullptr) *error = "every path must be a string";
                return false;
            }
            array.push_back(entry.get<std::string>());
        }
        normalized = std::move(array);
        return true;
    }

    if (kind == "region") {
        if (raw.is_null()) {
            normalized = nullptr;
            return true;
        }
        if (!raw.is_string()) {
            if (error != nullptr) *error = "expected x,y,w,h";
            return false;
        }
        const std::string text = trim(raw.get<std::string>());
        if (text.empty()) {
            normalized = nullptr;
            return true;
        }
        if (!parse_region(text, error)) {
            return false;
        }
        normalized = text;
        return true;
    }

    // text and path are plain strings; an empty value is null only when the
    // schema's default is null, which is the Python coerce rule for text.
    if (kind == "text" || kind == "path" || field.type == "string" ||
        field.type == "path") {
        if (raw.is_null()) {
            if (field.default_value.is_null()) {
                normalized = nullptr;
                return true;
            }
            if (error != nullptr) *error = "needs a string";
            return false;
        }
        if (!raw.is_string()) {
            if (error != nullptr) *error = "needs a string";
            return false;
        }
        const std::string value = raw.get<std::string>();
        if (value.empty() && field.default_value.is_null()) {
            normalized = nullptr;
            return true;
        }
        normalized = value;
        return true;
    }

    if (error != nullptr) {
        *error = "unknown field kind " + kind;
    }
    return false;
}

bool is_live_key(std::string_view key) noexcept {
    return key == "translation.target" || key == "translation.scene" ||
           key == "capture.fps" || key == "capture.diff_threshold";
}

std::string effective_scope(const SettingsField& field) {
    if (field.scope != "live") {
        return "restart";
    }
    if (!is_live_key(field.key)) {
        return "restart";
    }
    return "live";
}

nlohmann::json live_command(const SettingsField& field, const nlohmann::json& value) {
    if (!is_live_key(field.key) || value.is_null()) {
        return nullptr;
    }
    if (field.key == "translation.target") {
        if (!value.is_string()) {
            return nullptr;
        }
        return nlohmann::json{
            {"cmd", "set_target_lang"},
            {"target_lang", value.get<std::string>()},
        };
    }
    if (field.key == "translation.scene") {
        if (!value.is_string()) {
            return nullptr;
        }
        return nlohmann::json{
            {"cmd", "set_scene"},
            {"scene", value.get<std::string>()},
        };
    }
    if (field.key == "capture.fps") {
        if (!value.is_number()) {
            return nullptr;
        }
        return nlohmann::json{
            {"cmd", "set_fps"},
            {"value", value.get<double>()},
        };
    }
    if (field.key == "capture.diff_threshold") {
        if (!value.is_number()) {
            return nullptr;
        }
        // The schema's 2.0 means 2 %; the engine's detector takes a 0-1
        // fraction, and 0.02 is its own default. Clamp to the engine's range.
        double fraction = value.get<double>() / 100.0;
        fraction = std::max(0.0, std::min(1.0, fraction));
        return nlohmann::json{
            {"cmd", "set_diff_threshold"},
            {"value", fraction},
        };
    }
    return nullptr;
}

nlohmann::json reload_corpus_command() {
    return nlohmann::json{{"cmd", "reload_corpus"}};
}

SettingsStore::SettingsStore(SettingsSchema schema, std::filesystem::path config_path)
    : schema_(std::move(schema)), path_(std::move(config_path)) {
    reset_defaults();
}

void SettingsStore::reset_defaults() {
    for (auto& group : schema_.groups_mutable()) {
        for (auto& field : group.fields) {
            field.value = field.default_value;
        }
    }
}

bool SettingsStore::load(std::vector<std::string>* warnings, std::string* error) {
    reset_defaults();
    std::ifstream file(path_, std::ios::binary);
    if (!file) {
        // A missing user file is the normal first-run case.
        std::error_code exists_error;
        if (!std::filesystem::exists(path_, exists_error)) {
            return true;
        }
        if (error != nullptr) {
            *error = "cannot read " + path_.string();
        }
        return false;
    }
    nlohmann::json document;
    try {
        file >> document;
    } catch (const nlohmann::json::exception& exception) {
        if (error != nullptr) {
            *error = path_.string() + ": " + exception.what();
        }
        return false;
    }
    if (!document.is_object()) {
        if (error != nullptr) {
            *error = path_.string() + ": expected a JSON object";
        }
        return false;
    }
    for (auto entry = document.begin(); entry != document.end(); ++entry) {
        SettingsField* field = schema_.find_mutable(entry.key());
        if (field == nullptr) {
            if (warnings != nullptr) {
                warnings->push_back("unknown key ignored: " + entry.key());
            }
            continue;
        }
        nlohmann::json normalized;
        std::string reason;
        if (!validate_setting(*field, entry.value(), normalized, &reason)) {
            if (warnings != nullptr) {
                warnings->push_back("ignored " + entry.key() + ": " + reason);
            }
            continue;
        }
        field->value = std::move(normalized);
    }
    return true;
}

nlohmann::json SettingsStore::value(std::string_view key) const {
    const SettingsField* field = schema_.find(key);
    return field != nullptr ? field->value : nlohmann::json(nullptr);
}

const nlohmann::json* SettingsStore::find_value(std::string_view key) const {
    const SettingsField* field = schema_.find(key);
    return field != nullptr ? &field->value : nullptr;
}

bool SettingsStore::set_value(
    std::string_view key,
    const nlohmann::json& value,
    std::string* error) {
    SettingsField* field = schema_.find_mutable(key);
    if (field == nullptr) {
        if (error != nullptr) {
            *error = "unknown key: " + std::string(key);
        }
        return false;
    }
    nlohmann::json normalized;
    if (!validate_setting(*field, value, normalized, error)) {
        return false;
    }
    field->value = std::move(normalized);
    return true;
}

bool SettingsStore::is_default(std::string_view key) const {
    const SettingsField* field = schema_.find(key);
    if (field == nullptr) {
        return true;
    }
    return field->value == field->default_value;
}

std::vector<std::string> SettingsStore::changed_keys() const {
    std::vector<std::string> changed;
    for (const auto& group : schema_.groups()) {
        for (const auto& field : group.fields) {
            if (field.value != field.default_value) {
                changed.push_back(field.key);
            }
        }
    }
    return changed;
}

nlohmann::json SettingsStore::persisted_document() const {
    nlohmann::json document = nlohmann::json::object();
    for (const auto& group : schema_.groups()) {
        for (const auto& field : group.fields) {
            if (field.value != field.default_value) {
                document[field.key] = field.value;
            }
        }
    }
    return document;
}

bool SettingsStore::save(std::string* error) {
    const std::filesystem::path parent = path_.parent_path();
    if (!parent.empty()) {
        std::error_code directory_error;
        std::filesystem::create_directories(parent, directory_error);
        if (directory_error) {
            if (error != nullptr) {
                *error = "cannot create " + parent.string() + ": " +
                         directory_error.message();
            }
            return false;
        }
    }

    const std::filesystem::path temporary = path_.string() + ".tmp";
    {
        std::ofstream file(temporary, std::ios::binary | std::ios::trunc);
        if (!file) {
            if (error != nullptr) {
                *error = "cannot write " + temporary.string();
            }
            return false;
        }
        file << persisted_document().dump(2) << '\n';
        if (!file) {
            file.close();
            std::filesystem::remove(temporary);
            if (error != nullptr) {
                *error = "cannot write " + temporary.string();
            }
            return false;
        }
    }

#if defined(_WIN32)
    if (!MoveFileExW(
            temporary.wstring().c_str(),
            path_.wstring().c_str(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        const DWORD code = GetLastError();
        std::filesystem::remove(temporary);
        if (error != nullptr) {
            *error = "cannot replace " + path_.string() + " (error " +
                     std::to_string(code) + ")";
        }
        return false;
    }
#else
    std::error_code rename_error;
    std::filesystem::rename(temporary, path_, rename_error);
    if (rename_error) {
        std::filesystem::remove(temporary);
        if (error != nullptr) {
            *error = "cannot replace " + path_.string() + ": " +
                     rename_error.message();
        }
        return false;
    }
#endif
    return true;
}

std::string SettingsStore::display_text(
    const SettingsField& field,
    const nlohmann::json& value) {
    if (value.is_null()) {
        return std::string();
    }
    if (field.kind == "bool") {
        return value.get<bool>() ? "true" : "false";
    }
    if (field.kind == "int" && value.is_number_integer()) {
        return std::to_string(value.get<long long>());
    }
    if (field.kind == "float" && value.is_number()) {
        std::ostringstream stream;
        stream << value.get<double>();
        return stream.str();
    }
    if (field.kind == "path_list" && value.is_array()) {
        std::string text;
        for (std::size_t index = 0; index < value.size(); ++index) {
            if (index > 0) {
                text += ", ";
            }
            text += value[index].get<std::string>();
        }
        return text;
    }
    if (value.is_string()) {
        return value.get<std::string>();
    }
    return value.dump();
}

bool SettingsStore::parse_text(
    const SettingsField& field,
    const std::string& text,
    nlohmann::json& value,
    std::string* error) {
    const std::string trimmed = trim(text);
    if (field.kind == "bool") {
        std::string lowered = trimmed;
        std::transform(lowered.begin(), lowered.end(), lowered.begin(), [](char c) {
            return static_cast<char>(std::tolower(c));
        });
        if (lowered == "true" || lowered == "1" || lowered == "yes" || lowered == "on") {
            value = true;
            return true;
        }
        if (lowered == "false" || lowered == "0" || lowered == "no" || lowered == "off") {
            value = false;
            return true;
        }
        if (error != nullptr) *error = "needs true or false";
        return false;
    }
    if (field.kind == "int" || field.kind == "float") {
        if (trimmed.empty()) {
            if (field.default_value.is_null()) {
                value = nullptr;
                return true;
            }
            if (error != nullptr) *error = "needs a number";
            return false;
        }
        if (field.kind == "int") {
            try {
                std::size_t used = 0;
                const long long number = std::stoll(trimmed, &used);
                if (used != trimmed.size()) {
                    if (error != nullptr) *error = "needs a whole number";
                    return false;
                }
                value = number;
            } catch (const std::exception&) {
                if (error != nullptr) *error = "needs a whole number";
                return false;
            }
        } else {
            try {
                std::size_t used = 0;
                const double number = std::stod(trimmed, &used);
                if (used != trimmed.size()) {
                    if (error != nullptr) *error = "needs a number";
                    return false;
                }
                value = number;
            } catch (const std::exception&) {
                if (error != nullptr) *error = "needs a number";
                return false;
            }
        }
        return validate_setting(field, value, value, error);
    }
    if (field.kind == "enum") {
        if (trimmed.empty() && field.default_value.is_null()) {
            value = nullptr;
            return true;
        }
        value = trimmed;
        return validate_setting(field, value, value, error);
    }
    if (field.kind == "path_list") {
        value = split_path_list(trimmed);
        return true;
    }
    if (field.kind == "region") {
        if (trimmed.empty()) {
            value = nullptr;
            return true;
        }
        value = trimmed;
        return validate_setting(field, value, value, error);
    }
    // text / path
    if (trimmed.empty() && field.default_value.is_null()) {
        value = nullptr;
        return true;
    }
    value = text;
    return validate_setting(field, value, value, error);
}

}  // namespace watashi::ui
