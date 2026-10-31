// The declarative presentation spec, parsed and validated for the native
// overlay.
//
// The wire format is the one in docs/presentation-spec.md and
// config/presentation.json. This module is the single reader of that format:
// it knows the defaults, rejects illegal values with a line and column, ignores
// keys it does not use, and keeps the last valid spec when a file is broken.
//
// It is pure data plus the file watcher. Nothing here draws or touches a
// window, so every field can be asserted on a machine without a display.

#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace watashi::ui {

struct Rgb {
    std::uint8_t r = 255;
    std::uint8_t g = 255;
    std::uint8_t b = 255;
};

enum class Anchor {
    kTopLeft,
    kTopCenter,
    kTopRight,
    kMiddleLeft,
    kMiddleCenter,
    kMiddleRight,
    kBottomLeft,
    kBottomCenter,
    kBottomRight,
};

enum class Align { kLeft, kCenter, kRight };

/// Only ``bar`` and ``hidden`` change today's drawing; the others are accepted
/// so a documented preset file loads, and degrade to a bar block.
enum class LayoutMode { kBar, kLines, kInplace, kPanel, kHidden };

enum class BackgroundKind { kNone, kPlate };

struct FontSpec {
    /// Empty means the built-in CJK-capable face.
    std::string family;
    int size = 20;
    bool bold = false;
    bool italic = false;
};

struct OutlineSpec {
    Rgb color{0, 0, 0};
    int width = 2;
};

struct ElementSpec {
    std::string role = "target";
    int order = 0;
    FontSpec font;
    Rgb color{255, 255, 255};
    double opacity = 1.0;
    bool has_outline = false;
    OutlineSpec outline;
    /// 0 means unlimited.
    int max_lines = 0;
};

struct LayoutSpec {
    LayoutMode mode = LayoutMode::kBar;
    Anchor anchor = Anchor::kBottomCenter;
    /// Logical pixels, applied after anchoring.
    int offset_x = 0;
    int offset_y = -90;
    double max_width_ratio = 0.9;
    int line_gap = 6;
    Align align = Align::kCenter;
};

struct BackgroundSpec {
    BackgroundKind kind = BackgroundKind::kPlate;
    Rgb color{0, 0, 0};
    double opacity = 0.72;
    int radius = 8;
    /// Logical pixels.
    int padding_x = 12;
    int padding_y = 8;
};

struct ConfidenceSpec {
    double dim_below = 0.5;
    double dim_opacity = 0.6;
};

struct PresentationSpec {
    int version = 1;
    std::string name = "bar";
    LayoutSpec layout;
    std::vector<ElementSpec> elements;
    BackgroundSpec background;
    ConfidenceSpec confidence;
    bool per_line = false;

    /// The built-in `bar` preset, field for field the shipped
    /// config/presentation.json. Used when no file exists or a file is invalid.
    [[nodiscard]] static PresentationSpec bar();

    [[nodiscard]] const ElementSpec* find(std::string_view role) const;
    [[nodiscard]] std::vector<ElementSpec> ordered_elements() const;
};

struct SpecError {
    int line = 1;
    int column = 1;
    std::string message;
};

struct SpecLoad {
    bool ok = false;
    PresentationSpec spec;
    SpecError error;
};

/// Parse spec text. On failure ``spec`` is the built-in bar and ``error``
/// carries the first problem with a 1-based line and column. ``file_name`` is
/// only used for locating a field in the raw text, never echoed back.
[[nodiscard]] SpecLoad parse_presentation_text(
    std::string_view text,
    std::string_view file_name);

[[nodiscard]] SpecLoad load_presentation_file(const std::string& path);

/// ``<file>: <message> (line L, column C)``.
[[nodiscard]] std::string format_spec_error(
    const std::string& file_name,
    const SpecError& error);

/// mtime+size watcher around one spec file.
///
/// ``poll`` is throttled to 400 ms and only reads the file when the stat
/// changed, so a half-written file is retried on the next save and an invalid
/// file never replaces the last good spec. Every call must happen on one
/// thread (the window thread).
class PresentationWatcher {
public:
    PresentationWatcher() = default;
    explicit PresentationWatcher(std::string path);

    /// Start watching ``path``, loading it immediately. An empty path means
    /// "built-in bar, nothing to watch".
    void open(std::string path);

    /// Returns true when the active spec changed (a valid reload).
    bool poll(double now_seconds);

    [[nodiscard]] bool has_file() const noexcept { return !path_.empty(); }
    [[nodiscard]] const std::string& path() const noexcept { return path_; }
    [[nodiscard]] const PresentationSpec& spec() const noexcept { return spec_; }
    [[nodiscard]] std::uint64_t revision() const noexcept { return revision_; }
    /// Empty after a valid load. Persistent until a later read succeeds, so the
    /// status line shows one error rather than one per poll.
    [[nodiscard]] const std::string& error() const noexcept { return error_; }

private:
    struct Stat {
        std::int64_t mtime = 0;
        std::uintmax_t size = 0;
    };

    [[nodiscard]] std::optional<Stat> read_stat() const;
    void reload();

    std::string path_;
    PresentationSpec spec_ = PresentationSpec::bar();
    std::uint64_t revision_ = 0;
    std::string error_;
    std::uint64_t last_mtime_ = 0;
    std::uintmax_t last_size_ = 0;
    bool have_stat_ = false;
    double next_check_ = 0.0;
};

}  // namespace watashi::ui
