#include "watashi/ui/presentation.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <string>
#include <string_view>
#include <utility>

#include <nlohmann/json.hpp>

namespace watashi::ui {
namespace {

constexpr double kPollSeconds = 0.4;

struct Cursor {
    int line = 1;
    int column = 1;
};

/// Position of the first occurrence of ``"key"``. nlohmann does not keep source
/// positions for semantic errors, and pointing at the key is enough to find the
/// offending line in a hand-edited file.
Cursor locate_key(std::string_view text, std::string_view key) {
    const std::string needle = "\"" + std::string(key) + "\"";
    const auto position = text.find(needle);
    Cursor cursor;
    if (position == std::string_view::npos) {
        return cursor;
    }
    for (std::size_t index = 0; index < position; ++index) {
        if (text[index] == '\n') {
            ++cursor.line;
            cursor.column = 1;
        } else {
            ++cursor.column;
        }
    }
    return cursor;
}

/// 1-based line/column from nlohmann's 1-based byte offset.
Cursor cursor_from_byte(std::string_view text, std::size_t byte) {
    Cursor cursor;
    for (std::size_t index = 0; index + 1 < byte && index < text.size(); ++index) {
        if (text[index] == '\n') {
            ++cursor.line;
            cursor.column = 1;
        } else {
            ++cursor.column;
        }
    }
    return cursor;
}

bool parse_hex(std::string_view value, Rgb& out) {
    if (value.size() != 7 || value[0] != '#') {
        return false;
    }
    std::array<unsigned, 3> channels{};
    for (int channel = 0; channel < 3; ++channel) {
        const auto pair = value.substr(1 + channel * 2, 2);
        for (char character : pair) {
            if (!std::isxdigit(static_cast<unsigned char>(character))) {
                return false;
            }
        }
        channels[channel] = static_cast<unsigned>(
            std::stoul(std::string(pair), nullptr, 16));
    }
    out.r = static_cast<std::uint8_t>(channels[0]);
    out.g = static_cast<std::uint8_t>(channels[1]);
    out.b = static_cast<std::uint8_t>(channels[2]);
    return true;
}

bool anchor_from(std::string_view value, Anchor& out) {
    static const std::array<std::pair<std::string_view, Anchor>, 9> kAnchors{{
        {"top-left", Anchor::kTopLeft},
        {"top-center", Anchor::kTopCenter},
        {"top-right", Anchor::kTopRight},
        {"middle-left", Anchor::kMiddleLeft},
        {"middle-center", Anchor::kMiddleCenter},
        {"middle-right", Anchor::kMiddleRight},
        {"bottom-left", Anchor::kBottomLeft},
        {"bottom-center", Anchor::kBottomCenter},
        {"bottom-right", Anchor::kBottomRight},
    }};
    for (const auto& entry : kAnchors) {
        if (entry.first == value) {
            out = entry.second;
            return true;
        }
    }
    return false;
}

bool align_from(std::string_view value, Align& out) {
    if (value == "left") {
        out = Align::kLeft;
        return true;
    }
    if (value == "center") {
        out = Align::kCenter;
        return true;
    }
    if (value == "right") {
        out = Align::kRight;
        return true;
    }
    return false;
}

bool mode_from(std::string_view value, LayoutMode& out) {
    if (value == "bar") {
        out = LayoutMode::kBar;
        return true;
    }
    if (value == "lines") {
        out = LayoutMode::kLines;
        return true;
    }
    if (value == "inplace") {
        out = LayoutMode::kInplace;
        return true;
    }
    if (value == "panel") {
        out = LayoutMode::kPanel;
        return true;
    }
    if (value == "hidden") {
        out = LayoutMode::kHidden;
        return true;
    }
    return false;
}

bool role_known(std::string_view role) {
    return role == "source" || role == "target" || role == "trace" ||
           role == "previous";
}

class Parser {
public:
    explicit Parser(std::string_view text) : text_(text) {}

    bool fail(std::string_view key, std::string message) {
        const Cursor cursor = locate_key(text_, key);
        error_ = SpecError{cursor.line, cursor.column, std::move(message)};
        return false;
    }

    [[nodiscard]] const SpecError& error() const { return error_; }

    bool get_int(const nlohmann::json& object,
                 const char* key,
                 int minimum,
                 int maximum,
                 int& out) {
        const auto found = object.find(key);
        if (found == object.end()) {
            return true;
        }
        if (!found->is_number_integer()) {
            return fail(key, std::string(key) + " must be an integer");
        }
        const int value = found->get<int>();
        if (value < minimum || value > maximum) {
            return fail(
                key,
                std::string(key) + " must be between " + std::to_string(minimum) +
                    " and " + std::to_string(maximum));
        }
        out = value;
        return true;
    }

    bool get_number(const nlohmann::json& object,
                    const char* key,
                    double minimum,
                    double maximum,
                    double& out) {
        const auto found = object.find(key);
        if (found == object.end()) {
            return true;
        }
        if (!found->is_number()) {
            return fail(key, std::string(key) + " must be a number");
        }
        const double value = found->get<double>();
        if (value < minimum || value > maximum) {
            return fail(
                key,
                std::string(key) + " must be between " + std::to_string(minimum) +
                    " and " + std::to_string(maximum));
        }
        out = value;
        return true;
    }

    bool get_bool(const nlohmann::json& object, const char* key, bool& out) {
        const auto found = object.find(key);
        if (found == object.end()) {
            return true;
        }
        if (!found->is_boolean()) {
            return fail(key, std::string(key) + " must be true or false");
        }
        out = found->get<bool>();
        return true;
    }

    bool get_string(const nlohmann::json& object,
                    const char* key,
                    bool allow_null,
                    std::string& out) {
        const auto found = object.find(key);
        if (found == object.end()) {
            return true;
        }
        if (found->is_null() && allow_null) {
            return true;
        }
        if (!found->is_string()) {
            return fail(key, std::string(key) + " must be a string");
        }
        out = found->get<std::string>();
        return true;
    }

    bool get_color(const nlohmann::json& object, const char* key, Rgb& out) {
        const auto found = object.find(key);
        if (found == object.end()) {
            return true;
        }
        if (!found->is_string()) {
            return fail(key, std::string(key) + " must be a #rrggbb string");
        }
        if (!parse_hex(found->get<std::string>(), out)) {
            return fail(key, std::string(key) + " must be a #rrggbb colour");
        }
        return true;
    }

    bool get_offset(const nlohmann::json& object, int& first, int& second) {
        const auto found = object.find("offset");
        if (found == object.end()) {
            return true;
        }
        if (!found->is_array() || found->size() != 2 ||
            !(*found)[0].is_number_integer() || !(*found)[1].is_number_integer()) {
            return fail("offset", "offset must be [x, y] integers");
        }
        first = (*found)[0].get<int>();
        second = (*found)[1].get<int>();
        return true;
    }

    bool get_padding(const nlohmann::json& object, int& x, int& y) {
        const auto found = object.find("padding");
        if (found == object.end()) {
            return true;
        }
        if (!found->is_array() || found->size() != 2 ||
            !(*found)[0].is_number_integer() || !(*found)[1].is_number_integer()) {
            return fail("padding", "padding must be [x, y] integers");
        }
        x = (*found)[0].get<int>();
        y = (*found)[1].get<int>();
        if (x < 0 || y < 0) {
            return fail("padding", "padding must not be negative");
        }
        return true;
    }

private:
    std::string_view text_;
    SpecError error_;
};

bool parse_font(Parser& parser, const nlohmann::json& element, FontSpec& font) {
    const auto found = element.find("font");
    if (found == element.end()) {
        return true;
    }
    if (!found->is_object()) {
        return parser.fail("font", "font must be an object");
    }
    if (!parser.get_string(*found, "family", true, font.family)) {
        return false;
    }
    if (!parser.get_int(*found, "size", 4, 256, font.size)) {
        return false;
    }
    if (!parser.get_bool(*found, "bold", font.bold)) {
        return false;
    }
    if (!parser.get_bool(*found, "italic", font.italic)) {
        return false;
    }
    return true;
}

bool parse_element(Parser& parser, const nlohmann::json& value, ElementSpec& element) {
    if (!value.is_object()) {
        return parser.fail("elements", "each element must be an object");
    }
    if (!parser.get_string(value, "role", false, element.role)) {
        return false;
    }
    if (!role_known(element.role)) {
        return parser.fail(
            "role",
            "unknown element role \"" + element.role +
                "\" (expected source, target, trace or previous)");
    }
    if (!parser.get_int(value, "order", -1000, 1000, element.order)) {
        return false;
    }
    if (!parse_font(parser, value, element.font)) {
        return false;
    }
    if (!parser.get_color(value, "color", element.color)) {
        return false;
    }
    if (!parser.get_number(value, "opacity", 0.0, 1.0, element.opacity)) {
        return false;
    }
    if (!parser.get_int(value, "max_lines", 0, 1000, element.max_lines)) {
        return false;
    }

    const auto outline = value.find("outline");
    if (outline == value.end() || outline->is_null()) {
        return true;
    }
    if (!outline->is_object()) {
        return parser.fail("outline", "outline must be an object or null");
    }
    OutlineSpec parsed;
    if (!parser.get_color(*outline, "color", parsed.color)) {
        return false;
    }
    if (!parser.get_int(*outline, "width", 0, 32, parsed.width)) {
        return false;
    }
    element.has_outline = parsed.width > 0;
    element.outline = parsed;
    return true;
}

bool parse_layout(Parser& parser, const nlohmann::json& value, LayoutSpec& layout) {
    if (!value.is_object()) {
        return parser.fail("layout", "layout must be an object");
    }
    const auto mode = value.find("mode");
    if (mode != value.end()) {
        if (!mode->is_string()) {
            return parser.fail("mode", "mode must be a string");
        }
        const std::string text = mode->get<std::string>();
        LayoutMode parsed = layout.mode;
        if (!mode_from(text, parsed)) {
            return parser.fail("mode", "unknown layout mode \"" + text + "\"");
        }
        layout.mode = parsed;
    }
    const auto anchor = value.find("anchor");
    if (anchor != value.end()) {
        if (!anchor->is_string()) {
            return parser.fail("anchor", "anchor must be a string");
        }
        const std::string text = anchor->get<std::string>();
        Anchor parsed = layout.anchor;
        if (!anchor_from(text, parsed)) {
            return parser.fail("anchor", "unknown anchor \"" + text + "\"");
        }
        layout.anchor = parsed;
    }
    const auto align = value.find("align");
    if (align != value.end()) {
        if (!align->is_string()) {
            return parser.fail("align", "align must be a string");
        }
        const std::string text = align->get<std::string>();
        Align parsed = layout.align;
        if (!align_from(text, parsed)) {
            return parser.fail("align", "unknown alignment \"" + text + "\"");
        }
        layout.align = parsed;
    }
    if (!parser.get_offset(value, layout.offset_x, layout.offset_y)) {
        return false;
    }
    if (!parser.get_number(value, "max_width_ratio", 0.01, 1.0, layout.max_width_ratio)) {
        return false;
    }
    if (!parser.get_int(value, "line_gap", 0, 512, layout.line_gap)) {
        return false;
    }
    return true;
}

bool parse_background(
    Parser& parser,
    const nlohmann::json& value,
    BackgroundSpec& background) {
    if (!value.is_object()) {
        return parser.fail("background", "background must be an object");
    }
    const auto kind = value.find("kind");
    if (kind != value.end()) {
        if (!kind->is_string()) {
            return parser.fail("kind", "background kind must be a string");
        }
        const std::string text = kind->get<std::string>();
        if (text == "plate") {
            background.kind = BackgroundKind::kPlate;
        } else if (text == "none" || text == "transparent") {
            background.kind = BackgroundKind::kNone;
        } else {
            return parser.fail("kind", "unknown background kind \"" + text + "\"");
        }
    }
    if (!parser.get_color(value, "color", background.color)) {
        return false;
    }
    if (!parser.get_number(value, "opacity", 0.0, 1.0, background.opacity)) {
        return false;
    }
    if (!parser.get_int(value, "radius", 0, 512, background.radius)) {
        return false;
    }
    if (!parser.get_padding(value, background.padding_x, background.padding_y)) {
        return false;
    }
    return true;
}

bool parse_confidence(
    Parser& parser,
    const nlohmann::json& value,
    ConfidenceSpec& confidence) {
    if (!value.is_object()) {
        return parser.fail("confidence", "confidence must be an object");
    }
    if (!parser.get_number(value, "dim_below", 0.0, 1.0, confidence.dim_below)) {
        return false;
    }
    if (!parser.get_number(value, "dim_opacity", 0.0, 1.0, confidence.dim_opacity)) {
        return false;
    }
    return true;
}

}  // namespace

PresentationSpec PresentationSpec::bar() {
    PresentationSpec spec;
    spec.name = "bar";
    spec.layout = LayoutSpec{};
    spec.elements = {
        ElementSpec{"previous", 0, FontSpec{"", 13, false, false}, Rgb{0x7d, 0x90, 0x9f},
                    0.75, false, OutlineSpec{}, 0},
        ElementSpec{"source", 1, FontSpec{"", 16, false, false}, Rgb{0xc9, 0xd6, 0xe0},
                    0.9, false, OutlineSpec{}, 0},
        ElementSpec{"target", 2, FontSpec{"", 24, true, false}, Rgb{255, 255, 255},
                    1.0, false, OutlineSpec{}, 0},
    };
    spec.background = BackgroundSpec{};
    spec.confidence = ConfidenceSpec{};
    return spec;
}

const ElementSpec* PresentationSpec::find(std::string_view role) const {
    for (const auto& element : elements) {
        if (element.role == role) {
            return &element;
        }
    }
    return nullptr;
}

std::vector<ElementSpec> PresentationSpec::ordered_elements() const {
    std::vector<ElementSpec> ordered = elements;
    std::stable_sort(
        ordered.begin(), ordered.end(),
        [](const ElementSpec& left, const ElementSpec& right) {
            return left.order < right.order;
        });
    return ordered;
}

SpecLoad parse_presentation_text(
    std::string_view text,
    std::string_view /*file_name*/) {
    SpecLoad result;
    result.spec = PresentationSpec::bar();

    nlohmann::json raw;
    try {
        raw = nlohmann::json::parse(text.begin(), text.end());
    } catch (const nlohmann::json::parse_error& error) {
        const Cursor cursor = cursor_from_byte(text, error.byte);
        result.error = SpecError{cursor.line, cursor.column, error.what()};
        return result;
    } catch (const nlohmann::json::exception& error) {
        result.error = SpecError{1, 1, error.what()};
        return result;
    }

    if (!raw.is_object()) {
        result.error = SpecError{1, 1, "the top level must be a JSON object"};
        return result;
    }

    Parser parser(text);
    PresentationSpec spec = PresentationSpec::bar();

    if (!parser.get_int(raw, "version", 0, 1000, spec.version)) {
        result.error = parser.error();
        return result;
    }
    if (!parser.get_string(raw, "name", false, spec.name)) {
        result.error = parser.error();
        return result;
    }

    const auto layout = raw.find("layout");
    if (layout != raw.end() && !parse_layout(parser, *layout, spec.layout)) {
        result.error = parser.error();
        return result;
    }

    const auto elements = raw.find("elements");
    if (elements != raw.end()) {
        if (!elements->is_array()) {
            parser.fail("elements", "elements must be an array");
            result.error = parser.error();
            return result;
        }
        if (!elements->empty()) {
            std::vector<ElementSpec> parsed;
            for (const auto& element : *elements) {
                ElementSpec item;
                if (!parse_element(parser, element, item)) {
                    result.error = parser.error();
                    return result;
                }
                parsed.push_back(std::move(item));
            }
            spec.elements = std::move(parsed);
        }
    }

    const auto background = raw.find("background");
    if (background != raw.end() &&
        !parse_background(parser, *background, spec.background)) {
        result.error = parser.error();
        return result;
    }

    const auto confidence = raw.find("confidence");
    if (confidence != raw.end() &&
        !parse_confidence(parser, *confidence, spec.confidence)) {
        result.error = parser.error();
        return result;
    }

    if (!parser.get_bool(raw, "per_line", spec.per_line)) {
        result.error = parser.error();
        return result;
    }

    result.ok = true;
    result.spec = std::move(spec);
    return result;
}

SpecLoad load_presentation_file(const std::string& path) {
    std::ifstream file(std::filesystem::path(path), std::ios::binary);
    if (!file) {
        SpecLoad result;
        result.error = SpecError{1, 1, "cannot open presentation file"};
        return result;
    }
    const std::string text((std::istreambuf_iterator<char>(file)),
                           std::istreambuf_iterator<char>());
    return parse_presentation_text(text, path);
}

std::string format_spec_error(
    const std::string& file_name,
    const SpecError& error) {
    return file_name + ": " + error.message + " (line " +
           std::to_string(error.line) + ", column " +
           std::to_string(error.column) + ")";
}

PresentationWatcher::PresentationWatcher(std::string path) {
    open(std::move(path));
}

void PresentationWatcher::open(std::string path) {
    path_ = std::move(path);
    spec_ = PresentationSpec::bar();
    revision_ = 0;
    error_.clear();
    last_mtime_ = 0;
    last_size_ = 0;
    have_stat_ = false;
    next_check_ = 0.0;

    if (path_.empty()) {
        return;
    }
    if (const auto stat = read_stat()) {
        last_mtime_ = static_cast<std::uint64_t>(stat->mtime);
        last_size_ = stat->size;
        have_stat_ = true;
    }
    reload();
}

std::optional<PresentationWatcher::Stat> PresentationWatcher::read_stat() const {
    std::error_code error;
    const auto mtime = std::filesystem::last_write_time(path_, error);
    if (error) {
        return std::nullopt;
    }
    const auto size = std::filesystem::file_size(path_, error);
    Stat stat;
    stat.mtime = static_cast<std::int64_t>(mtime.time_since_epoch().count());
    stat.size = error ? 0 : size;
    return stat;
}

void PresentationWatcher::reload() {
    const SpecLoad loaded = load_presentation_file(path_);
    if (loaded.ok) {
        spec_ = loaded.spec;
        ++revision_;
        error_.clear();
        return;
    }
    error_ = format_spec_error(path_, loaded.error);
}

bool PresentationWatcher::poll(double now_seconds) {
    if (path_.empty()) {
        return false;
    }
    if (now_seconds < next_check_) {
        return false;
    }
    next_check_ = now_seconds + kPollSeconds;

    const auto stat = read_stat();
    if (!stat) {
        have_stat_ = false;
        const std::string message = path_ + ": presentation file not found";
        if (error_ != message) {
            error_ = message;
        }
        return false;
    }
    if (have_stat_ && stat->mtime == static_cast<std::int64_t>(last_mtime_) &&
        stat->size == last_size_) {
        return false;
    }
    last_mtime_ = static_cast<std::uint64_t>(stat->mtime);
    last_size_ = stat->size;
    have_stat_ = true;

    const std::uint64_t before = revision_;
    reload();
    return revision_ != before;
}

}  // namespace watashi::ui
