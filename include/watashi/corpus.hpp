// Layered corpus with conditional senses and hot reload.
//
// Ported from prototype/watashi/translate.py by behaviour, not line by line.
// The rules of that module are the contract: user > domain > general, longest
// match wins, a corpus hit is authoritative, and every span carries provenance.

#pragma once

#include <cstdint>
#include <filesystem>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <regex>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

namespace watashi {

enum class Layer { User, Domain, General };

/// What has to hold around a term before its entry may be used.
struct Conditions {
    std::optional<std::string> line;    // regex, matched against the whole line
    std::vector<std::string> near;      // any of these, outside the matched term
    std::optional<std::string> window;  // regex, matched against the window title
    // Compiled at load; a pattern that does not compile is dropped like the
    // Python loader drops it (the rest of the conditions still apply).
    std::shared_ptr<const std::regex> line_regex;
    std::shared_ptr<const std::regex> window_regex;

    [[nodiscard]] bool empty() const;
};

/// Everything a lookup may depend on besides the text itself.
struct Context {
    std::string target_lang;
    std::string scene;
    std::string window;
};

/// One corpus entry (one meaning of one source term).
struct Entry {
    std::string source;
    std::u32string normalized_source;
    std::string target;
    Layer layer = Layer::General;
    std::string origin;  // "corpus:<file stem>"
    int priority = 0;
    std::string pos;
    std::string domain;
    std::string lang;    // "" = usable for every target
    std::shared_ptr<const Conditions> conditions;
};

/// A translated slice of the input, with provenance.
struct Span {
    std::string source;
    std::string target;
    std::string origin;  // "corpus:...", "rule:...", "literal"
    double confidence = 0.0;
    std::string rule_id;

    [[nodiscard]] bool explained() const;
};

/// Translation result for one line.
struct Outcome {
    std::string source_text;
    std::string target_text;
    std::vector<Span> spans;
    std::string backend = "corpus+rules";

    [[nodiscard]] double coverage() const;
    [[nodiscard]] double confidence() const;
    [[nodiscard]] std::string trace() const;
};

class RuleSet;  // rules.hpp

/// The corpus itself: files in, entries out, reload on mtime change.
class CorpusStore {
public:
    struct Options {
        std::vector<std::string> user_dirs;
        std::vector<std::string> domain_dirs;
        std::vector<std::string> general_dirs;
        std::vector<std::string> rule_files;
        bool auto_reload = true;
        double reload_interval_s = 0.5;
    };

    explicit CorpusStore(Options options);
    ~CorpusStore();
    CorpusStore(const CorpusStore&) = delete;
    CorpusStore& operator=(const CorpusStore&) = delete;

    /// Translate one line with longest-match corpus spans, then rules.
    [[nodiscard]] Outcome translate(
        std::string_view text,
        const std::string& target_lang,
        const Context& context = {});

    /// The entry for one term. Used by the span scanner and by affix/morpheme
    /// rules, so a decomposed word still benefits from user vocabulary.
    [[nodiscard]] const Entry* lookup_exact(
        std::u32string_view term,
        const std::string& target_lang,
        const Context& context = {}) const;

    /// Reload when any corpus or rule file changed. Throttled unless forced.
    /// ``now`` overrides the monotonic clock, which is what makes the reload
    /// test deterministic; pass a negative value to use the real clock.
    bool reload_if_changed(double now = -1.0, bool force = false);

    /// Read every file again, replacing the vocabulary. A file that fails to
    /// parse keeps its last good contents instead of taking the store down.
    void load();

    [[nodiscard]] std::size_t size() const { return entries_.size(); }
    [[nodiscard]] std::size_t rule_count() const;
    [[nodiscard]] std::vector<std::string> rule_ids() const;
    [[nodiscard]] std::vector<std::string> languages() const;
    [[nodiscard]] std::vector<std::string> suppressed_sources() const;
    [[nodiscard]] std::uint64_t revision() const { return revision_; }
    [[nodiscard]] int reloads() const { return reloads_; }
    [[nodiscard]] double reload_interval_s() const { return reload_interval_; }
    void set_reload_interval_s(double value);
    [[nodiscard]] bool auto_reload() const { return auto_reload_; }
    void set_auto_reload(bool value) { auto_reload_ = value; }

    /// Entries that lost to another entry on the same source, language, scene
    /// and conditions. A diagnostic, not an error.
    [[nodiscard]] const std::vector<nlohmann::json>& conflicts() const { return conflicts_; }
    /// Entries the user layer hid from the shipped corpora.
    [[nodiscard]] std::size_t hidden_entry_count() const { return hidden_count_; }

    /// The last file that failed to parse, empty when everything loaded.
    [[nodiscard]] const std::string& last_error() const { return last_error_; }

    [[nodiscard]] const RuleSet& rules() const;

    /// Lower-case ASCII only, like the Python `normalize`: length preserving,
    /// which the span arithmetic depends on.
    [[nodiscard]] static std::u32string normalize(std::u32string_view text);

    /// The language part of a tag or NLLB code ("zh-CN" / "zho_Hans" -> "zho").
    [[nodiscard]] static std::string language_base(const std::string& tag);
    [[nodiscard]] static bool same_language(const std::string& left, const std::string& right);

private:
    struct View {
        std::map<std::u32string, std::vector<const Entry*>> entries;
        std::size_t max_key = 1;
    };

    void load_locked();
    void collect_files(
        const std::vector<std::string>& roots,
        const std::function<void(const std::filesystem::path&)>& visit) const;
    [[nodiscard]] const View& view_for(const std::string& target_lang) const;
    [[nodiscard]] const Entry* pick(
        const std::vector<const Entry*>& candidates,
        const Context& context,
        std::u32string_view text,
        std::size_t start,
        std::size_t length) const;

    Options options_;
    std::vector<std::unique_ptr<Entry>> entries_;
    std::map<std::u32string, std::vector<const Entry*>> untagged_;
    std::map<std::string, std::map<std::u32string, std::vector<const Entry*>>> tagged_;
    std::map<std::string, View> views_;
    std::vector<std::string> languages_;
    std::vector<nlohmann::json> conflicts_;
    std::size_t hidden_count_ = 0;
    std::vector<std::string> suppressed_;
    std::unique_ptr<RuleSet> rules_;
    std::map<std::string, nlohmann::ordered_json> last_good_;
    std::map<std::string, std::pair<double, std::uintmax_t>> mtimes_;
    std::string last_error_;
    std::uint64_t revision_ = 0;
    int reloads_ = 0;
    bool auto_reload_ = true;
    double reload_interval_ = 0.5;
    double last_check_ = -1e300;
};

}  // namespace watashi
