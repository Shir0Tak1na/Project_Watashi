// The generated settings document, merge/save semantics and the live mapping.
// No window is created here: the settings window renders the same model.

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <catch2/catch_approx.hpp>
#include <catch2/catch_test_macros.hpp>

#include <chrono>
#include <filesystem>
#include <fstream>
#include <random>
#include <set>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "watashi/ui/settings.hpp"

namespace {

using namespace watashi;

std::filesystem::path module_directory() {
    wchar_t module[32768]{};
    GetModuleFileNameW(nullptr, module, 32768);
    return std::filesystem::path(module).parent_path();
}

/// Walk up from the test binary until the generated document is found.
std::filesystem::path repo_root() {
    auto directory = module_directory();
    for (int step = 0; step < 6; ++step) {
        if (std::filesystem::exists(directory / "config" / "settings_ui.json")) {
            return directory;
        }
        directory = directory.parent_path();
    }
    return module_directory();
}

ui::SettingsSchema load_generated() {
    std::string error;
    ui::SettingsSchema schema = ui::SettingsSchema::load(
        repo_root() / "config" / "settings_ui.json", &error);
    INFO("schema error: " << error);
    return schema;
}

const ui::SettingsField& field(const ui::SettingsSchema& schema, const char* key) {
    const ui::SettingsField* found = schema.find(key);
    REQUIRE(found != nullptr);
    return *found;
}

nlohmann::json read_json(const std::filesystem::path& path) {
    std::ifstream file(path, std::ios::binary);
    nlohmann::json document;
    file >> document;
    return document;
}

void write_json(const std::filesystem::path& path, const nlohmann::json& document) {
    std::filesystem::create_directories(path.parent_path());
    std::ofstream file(path, std::ios::binary | std::ios::trunc);
    file << document.dump(2) << '\n';
}

/// A unique user-settings file that does not collide with a parallel process.
struct TempConfig {
    TempConfig() {
        std::random_device device;
        std::mt19937 generator(device());
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        path = module_directory() /
               ("watashi_user_" + std::to_string(stamp) + "_" +
                std::to_string(generator()) + ".json");
        std::filesystem::remove(path);
    }
    ~TempConfig() {
        std::filesystem::remove(path);
        std::filesystem::remove(path.string() + ".tmp");
    }

    std::filesystem::path path;
};

ui::SettingsStore make_store(const TempConfig& file) {
    return ui::SettingsStore(load_generated(), file.path);
}

std::string schema_error(const nlohmann::json& document) {
    std::string error;
    ui::SettingsSchema::from_json(document, &error);
    return error;
}

}  // namespace

TEST_CASE("the generated document loads every group and field", "[settings]") {
    const ui::SettingsSchema schema = load_generated();
    REQUIRE_FALSE(schema.empty());
    REQUIRE(schema.groups().size() == 10);
    REQUIRE(schema.field_count() == 79);

    std::set<std::string> keys;
    int live = 0;
    int restart = 0;
    for (const auto& group : schema.groups()) {
        REQUIRE_FALSE(group.id.empty());
        REQUIRE_FALSE(group.title.empty());
        for (const auto& item : group.fields) {
            REQUIRE(keys.insert(item.key).second);
            REQUIRE_FALSE(item.label.empty());
            REQUIRE_FALSE(item.description.empty());
            if (item.scope == "live") {
                ++live;
            } else {
                REQUIRE(item.scope == "restart");
                ++restart;
            }
        }
    }
    REQUIRE(live == 10);
    REQUIRE(restart == 69);
    REQUIRE(schema.groups().front().id == "capture");
}

TEST_CASE("generated labels, defaults and types match the Python schema",
          "[settings]") {
    const ui::SettingsSchema schema = load_generated();
    const auto& fps = field(schema, "capture.fps");
    REQUIRE(fps.label == "采集帧率");
    REQUIRE(fps.type == "float");
    REQUIRE(fps.default_value == 10.0);
    REQUIRE(fps.scope == "live");
    REQUIRE(fps.has_low);
    REQUIRE(fps.low == 1.0);

    const auto& target = field(schema, "translation.target");
    REQUIRE(target.label == "目标语言");
    REQUIRE(target.default_value == "zh-CN");

    const auto& diff = field(schema, "capture.diff_threshold");
    REQUIRE(diff.default_value == 2.0);

    const auto& monitor = field(schema, "capture.monitor");
    REQUIRE(monitor.label == "显示器");
    REQUIRE(monitor.type == "int");
    REQUIRE(monitor.default_value == 1);
    REQUIRE(monitor.has_high);
    REQUIRE(monitor.high == 8.0);

    const auto& limit = field(schema, "ocr.det_limit_type");
    REQUIRE(limit.type == "enum");
    REQUIRE(limit.choices.size() == 2);
    REQUIRE(limit.choices[0] == "max");
    REQUIRE(limit.choices[1] == "min");

    const auto& region = field(schema, "capture.region");
    REQUIRE(region.type == "string");
    REQUIRE(region.kind == "region");
    REQUIRE(region.default_value.is_null());

    const auto& presentation_file = field(schema, "overlay.presentation_file");
    REQUIRE(presentation_file.type == "path");
    REQUIRE(presentation_file.default_value.is_null());

    const auto& directories = field(schema, "plugins.directories");
    REQUIRE(directories.type == "path");
    REQUIRE(directories.kind == "path_list");
    REQUIRE(directories.default_value.is_array());
}

TEST_CASE("every field has a supported widget type and a scope", "[settings]") {
    const ui::SettingsSchema schema = load_generated();
    const std::set<std::string> types = {"bool", "int", "float", "enum", "string", "path"};
    for (const auto& group : schema.groups()) {
        for (const auto& item : group.fields) {
            REQUIRE(types.count(item.type) == 1);
            REQUIRE((item.scope == "live" || item.scope == "restart"));
            if (item.type == "enum") {
                REQUIRE_FALSE(item.choices.empty());
            }
        }
    }
}

TEST_CASE("a new field in the document appears automatically", "[settings]") {
    const nlohmann::json document = nlohmann::json{
        {"version", 1},
        {"groups", nlohmann::json::array({
            nlohmann::json{
                {"id", "test"},
                {"title", "Test"},
                {"summary", ""},
                {"fields", nlohmann::json::array({
                    nlohmann::json{
                        {"key", "test.enabled"},
                        {"label", "Enabled"},
                        {"type", "bool"},
                        {"default", true},
                        {"scope", "live"},
                        {"description", "A switch."},
                    },
                    nlohmann::json{
                        {"key", "test.future"},
                        {"label", "Future"},
                        {"type", "string"},
                        {"default", "x"},
                        {"scope", "restart"},
                        {"description", "Added to the schema only."},
                    },
                })},
            },
        })},
    };
    std::string error;
    ui::SettingsSchema schema = ui::SettingsSchema::from_json(document, &error);
    REQUIRE_FALSE(schema.empty());
    REQUIRE(schema.field_count() == 2);
    REQUIRE(schema.find("test.future") != nullptr);
    REQUIRE(schema.find("test.enabled")->kind == "bool");
}

TEST_CASE("a malformed settings document is rejected", "[settings]") {
    REQUIRE(schema_error(nlohmann::json::object()).find("groups") != std::string::npos);

    const nlohmann::json missing_label = nlohmann::json{
        {"groups", nlohmann::json::array({nlohmann::json{
            {"id", "g"}, {"title", "T"}, {"fields", nlohmann::json::array({
                nlohmann::json{
                    {"key", "a.b"}, {"type", "bool"}, {"default", true},
                    {"scope", "live"},
                },
            })},
        }})},
    };
    REQUIRE(schema_error(missing_label).find("label") != std::string::npos);

    const nlohmann::json bad_enum = nlohmann::json{
        {"groups", nlohmann::json::array({nlohmann::json{
            {"id", "g"}, {"title", "T"}, {"fields", nlohmann::json::array({
                nlohmann::json{
                    {"key", "a.b"}, {"label", "B"}, {"type", "enum"},
                    {"default", "x"}, {"scope", "live"}, {"choices",
                    nlohmann::json::array()},
                },
            })},
        }})},
    };
    REQUIRE(schema_error(bad_enum).find("choices") != std::string::npos);

    const nlohmann::json duplicate = nlohmann::json{
        {"groups", nlohmann::json::array({nlohmann::json{
            {"id", "g"}, {"title", "T"}, {"fields", nlohmann::json::array({
                nlohmann::json{
                    {"key", "a.b"}, {"label", "B"}, {"type", "bool"},
                    {"default", true}, {"scope", "live"},
                },
                nlohmann::json{
                    {"key", "a.b"}, {"label", "B2"}, {"type", "bool"},
                    {"default", false}, {"scope", "live"},
                },
            })},
        }})},
    };
    REQUIRE(schema_error(duplicate).find("duplicate") != std::string::npos);
}

TEST_CASE("defaults merge with config/user.json overrides", "[settings]") {
    TempConfig file;
    write_json(file.path, nlohmann::json{
        {"translation.target", "en"},
        {"capture.fps", 5},
    });
    ui::SettingsStore store = make_store(file);
    std::vector<std::string> warnings;
    std::string error;
    REQUIRE(store.load(&warnings, &error));
    REQUIRE(warnings.empty());
    REQUIRE(store.value("translation.target") == "en");
    REQUIRE(store.value("capture.fps") == 5);
    REQUIRE(store.value("capture.monitor") == 1);
    REQUIRE(store.value("capture.diff_threshold") == 2.0);
}

TEST_CASE("unknown keys are ignored and reported", "[settings]") {
    TempConfig file;
    write_json(file.path, nlohmann::json{
        {"not.a.key", 1},
        {"capture.fps", 6},
    });
    ui::SettingsStore store = make_store(file);
    std::vector<std::string> warnings;
    std::string error;
    REQUIRE(store.load(&warnings, &error));
    REQUIRE(warnings.size() == 1);
    REQUIRE(warnings.front().find("not.a.key") != std::string::npos);
    REQUIRE(store.value("capture.fps") == 6);
}

TEST_CASE("invalid values keep the default and warn", "[settings]") {
    TempConfig file;
    write_json(file.path, nlohmann::json{
        {"capture.fps", "fast"},
        {"capture.monitor", 99},
    });
    ui::SettingsStore store = make_store(file);
    std::vector<std::string> warnings;
    std::string error;
    REQUIRE(store.load(&warnings, &error));
    REQUIRE(warnings.size() == 2);
    REQUIRE(store.value("capture.fps") == 10.0);
    REQUIRE(store.value("capture.monitor") == 1);
}

TEST_CASE("set_value refuses an unknown key", "[settings]") {
    TempConfig file;
    ui::SettingsStore store = make_store(file);
    std::string error;
    REQUIRE_FALSE(store.set_value("not.a.key", 1, &error));
    REQUIRE(error.find("unknown") != std::string::npos);
}

TEST_CASE("set_value refuses an out-of-range value", "[settings]") {
    TempConfig file;
    ui::SettingsStore store = make_store(file);
    std::string error;
    REQUIRE_FALSE(store.set_value("capture.monitor", 99, &error));
    REQUIRE_FALSE(store.set_value("capture.fps", 0, &error));
    REQUIRE(store.set_value("translation.target", "en", &error));
    REQUIRE(store.value("translation.target") == "en");
}

TEST_CASE("only values that differ from the defaults are persisted", "[settings]") {
    TempConfig file;
    ui::SettingsStore store = make_store(file);
    std::string error;
    REQUIRE(store.set_value("translation.target", "en", &error));
    REQUIRE(store.set_value("capture.fps", 5, &error));

    const nlohmann::json document = store.persisted_document();
    REQUIRE(document.size() == 2);
    REQUIRE(document.contains("translation.target"));
    REQUIRE(document.contains("capture.fps"));
    REQUIRE_FALSE(document.contains("capture.monitor"));
    REQUIRE_FALSE(document.contains("translation.scene"));

    REQUIRE(store.save(&error));
    const nlohmann::json saved = read_json(file.path);
    REQUIRE(saved.size() == 2);
    REQUIRE(saved.at("capture.fps") == 5);
}

TEST_CASE("a defaults-only save writes an empty object", "[settings]") {
    TempConfig file;
    ui::SettingsStore store = make_store(file);
    std::string error;
    REQUIRE(store.save(&error));
    const nlohmann::json saved = read_json(file.path);
    REQUIRE(saved.is_object());
    REQUIRE(saved.empty());
}

TEST_CASE("save replaces the file atomically without a temp leftover", "[settings]") {
    TempConfig file;
    write_json(file.path, nlohmann::json{{"capture.fps", 3}});
    ui::SettingsStore store = make_store(file);
    std::string error;
    REQUIRE(store.set_value("capture.fps", 7, &error));
    REQUIRE(store.save(&error));
    REQUIRE_FALSE(std::filesystem::exists(file.path.string() + ".tmp"));
    const nlohmann::json saved = read_json(file.path);
    REQUIRE(saved.at("capture.fps") == 7);
}

TEST_CASE("reset to defaults clears what will be persisted", "[settings]") {
    TempConfig file;
    ui::SettingsStore store = make_store(file);
    std::string error;
    REQUIRE(store.set_value("translation.target", "en", &error));
    REQUIRE(store.save(&error));
    store.reset_defaults();
    REQUIRE(store.value("translation.target") == "zh-CN");
    REQUIRE(store.persisted_document().empty());
    REQUIRE(store.save(&error));
    const nlohmann::json saved = read_json(file.path);
    REQUIRE(saved.empty());
}

TEST_CASE("a saved file loads back into the next session", "[settings]") {
    TempConfig file;
    {
        ui::SettingsStore store = make_store(file);
        std::string error;
        REQUIRE(store.set_value("translation.target", "en", &error));
        REQUIRE(store.set_value("capture.fps", 4, &error));
        REQUIRE(store.save(&error));
    }
    ui::SettingsStore next = make_store(file);
    std::vector<std::string> warnings;
    std::string error;
    REQUIRE(next.load(&warnings, &error));
    REQUIRE(warnings.empty());
    REQUIRE(next.value("translation.target") == "en");
    REQUIRE(next.value("capture.fps") == 4);
}

TEST_CASE("target language maps to set_target_lang", "[settings]") {
    const ui::SettingsSchema schema = load_generated();
    const auto& target = field(schema, "translation.target");
    REQUIRE(ui::effective_scope(target) == "live");
    const nlohmann::json command = ui::live_command(target, "en");
    REQUIRE(command.at("cmd") == "set_target_lang");
    REQUIRE(command.at("target_lang") == "en");
}

TEST_CASE("scene maps to set_scene", "[settings]") {
    const ui::SettingsSchema schema = load_generated();
    const auto& scene = field(schema, "translation.scene");
    REQUIRE(ui::effective_scope(scene) == "live");
    const nlohmann::json command = ui::live_command(scene, "cultivation");
    REQUIRE(command.at("cmd") == "set_scene");
    REQUIRE(command.at("scene") == "cultivation");
}

TEST_CASE("fps maps to set_fps", "[settings]") {
    const ui::SettingsSchema schema = load_generated();
    const auto& fps = field(schema, "capture.fps");
    REQUIRE(ui::effective_scope(fps) == "live");
    const nlohmann::json command = ui::live_command(fps, 8);
    REQUIRE(command.at("cmd") == "set_fps");
    REQUIRE(command.at("value") == 8.0);
}

TEST_CASE("the diff threshold converts percent to the engine fraction",
          "[settings]") {
    const ui::SettingsSchema schema = load_generated();
    const auto& diff = field(schema, "capture.diff_threshold");
    REQUIRE(ui::effective_scope(diff) == "live");
    REQUIRE(ui::live_command(diff, 2.0).at("value") == Catch::Approx(0.02));
    REQUIRE(ui::live_command(diff, 100.0).at("value") == 1.0);
    REQUIRE(ui::live_command(diff, 200.0).at("value") == 1.0);
    REQUIRE(ui::live_command(diff, 0.0).at("value") == 0.0);
}

TEST_CASE("fields the engine cannot hot-apply are marked restart", "[settings]") {
    const ui::SettingsSchema schema = load_generated();
    // capture.region says live in the Python schema but C++ has no set_region.
    REQUIRE(field(schema, "capture.region").scope == "live");
    REQUIRE(ui::effective_scope(field(schema, "capture.region")) == "restart");
    REQUIRE(ui::effective_scope(field(schema, "corpus.auto_reload")) == "restart");
    REQUIRE(ui::effective_scope(field(schema, "capture.monitor")) == "restart");
    REQUIRE(ui::effective_scope(field(schema, "translation.target")) == "live");
}

TEST_CASE("reload corpus is a command of its own", "[settings]") {
    REQUIRE(ui::reload_corpus_command().at("cmd") == "reload_corpus");
}

TEST_CASE("each widget kind round-trips through its display text", "[settings]") {
    const ui::SettingsSchema schema = load_generated();
    const auto& enabled = field(schema, "plugins.enabled");
    REQUIRE(ui::SettingsStore::display_text(enabled, true) == "true");
    nlohmann::json parsed;
    std::string error;
    REQUIRE(ui::SettingsStore::parse_text(enabled, "false", parsed, &error));
    REQUIRE(parsed == false);

    const auto& threads = field(schema, "ocr.intra_op_threads");
    REQUIRE(ui::SettingsStore::display_text(threads, 4) == "4");
    REQUIRE(ui::SettingsStore::parse_text(threads, "5", parsed, &error));
    REQUIRE(parsed == 5);

    const auto& ratio = field(schema, "capture.region_ratio");
    REQUIRE(ui::SettingsStore::display_text(ratio, 0.18) == "0.18");
    REQUIRE(ui::SettingsStore::parse_text(ratio, "0.25", parsed, &error));
    REQUIRE(parsed == 0.25);

    const auto& limit = field(schema, "ocr.det_limit_type");
    REQUIRE(ui::SettingsStore::parse_text(limit, "min", parsed, &error));
    REQUIRE(parsed == "min");

    const auto& directories = field(schema, "plugins.directories");
    REQUIRE(ui::SettingsStore::display_text(
                directories, nlohmann::json::array({"a", "b"})) == "a, b");
    REQUIRE(ui::SettingsStore::parse_text(directories, "a, b", parsed, &error));
    REQUIRE(parsed == nlohmann::json::array({"a", "b"}));

    const auto& region = field(schema, "capture.region");
    REQUIRE(ui::SettingsStore::parse_text(region, "10,20,300,40", parsed, &error));
    REQUIRE(parsed == "10,20,300,40");
    REQUIRE_FALSE(ui::SettingsStore::parse_text(region, "10,20", parsed, &error));
    REQUIRE(ui::SettingsStore::parse_text(region, "", parsed, &error));
    REQUIRE(parsed.is_null());

    const auto& font = field(schema, "overlay.font_family");
    REQUIRE(ui::SettingsStore::parse_text(font, "", parsed, &error));
    REQUIRE(parsed.is_null());
}

TEST_CASE("optional numbers accept an empty control", "[settings]") {
    const ui::SettingsSchema schema = load_generated();
    const auto& side = field(schema, "ocr.det_limit_side_len");
    nlohmann::json parsed = "stale";
    std::string error;
    REQUIRE(ui::SettingsStore::parse_text(side, "", parsed, &error));
    REQUIRE(parsed.is_null());
    REQUIRE(ui::SettingsStore::parse_text(side, "736", parsed, &error));
    REQUIRE(parsed == 736);

    const auto& threads = field(schema, "ocr.intra_op_threads");
    REQUIRE_FALSE(ui::SettingsStore::parse_text(threads, "", parsed, &error));
    REQUIRE_FALSE(ui::SettingsStore::parse_text(threads, "1.5", parsed, &error));
}

TEST_CASE("text controls reject values the schema cannot represent", "[settings]") {
    const ui::SettingsSchema schema = load_generated();
    nlohmann::json parsed;
    std::string error;
    REQUIRE_FALSE(ui::SettingsStore::parse_text(
        field(schema, "ocr.det_limit_type"), "nope", parsed, &error));
    REQUIRE_FALSE(ui::SettingsStore::parse_text(
        field(schema, "capture.monitor"), "abc", parsed, &error));
    REQUIRE_FALSE(ui::SettingsStore::parse_text(
        field(schema, "capture.region_ratio"), "1.5", parsed, &error));
    REQUIRE_FALSE(ui::SettingsStore::parse_text(
        field(schema, "capture.region"), "1,2,0,4", parsed, &error));
}

TEST_CASE("path lists are normalized from arrays and comma text", "[settings]") {
    const ui::SettingsSchema schema = load_generated();
    const auto& directories = field(schema, "plugins.directories");
    nlohmann::json normalized;
    std::string error;
    REQUIRE(ui::validate_setting(
        directories, nlohmann::json::array({"a", "b"}), normalized, &error));
    REQUIRE(normalized == nlohmann::json::array({"a", "b"}));
    REQUIRE(ui::validate_setting(directories, "a,b", normalized, &error));
    REQUIRE(normalized == nlohmann::json::array({"a", "b"}));
    REQUIRE(ui::validate_setting(directories, nullptr, normalized, &error));
    REQUIRE(normalized == nlohmann::json::array());
    REQUIRE_FALSE(ui::validate_setting(
        directories, nlohmann::json::array({"a", 3}), normalized, &error));
}

TEST_CASE("an enum with a null default accepts the unset value", "[settings]") {
    const ui::SettingsSchema schema = load_generated();
    const auto& profile = field(schema, "profile");
    REQUIRE(profile.default_value.is_null());
    REQUIRE(profile.choices.size() == 3);
    nlohmann::json parsed;
    std::string error;
    REQUIRE(ui::SettingsStore::parse_text(profile, "", parsed, &error));
    REQUIRE(parsed.is_null());
    REQUIRE(ui::SettingsStore::parse_text(profile, "lean", parsed, &error));
    REQUIRE(parsed == "lean");
    REQUIRE_FALSE(ui::SettingsStore::parse_text(profile, "turbo", parsed, &error));
}

TEST_CASE("changed keys and default checks stay in step", "[settings]") {
    TempConfig file;
    ui::SettingsStore store = make_store(file);
    REQUIRE(store.is_default("translation.target"));
    REQUIRE(store.changed_keys().empty());
    std::string error;
    REQUIRE(store.set_value("translation.target", "en", &error));
    REQUIRE_FALSE(store.is_default("translation.target"));
    const auto changed = store.changed_keys();
    REQUIRE(changed.size() == 1);
    REQUIRE(changed.front() == "translation.target");
}
