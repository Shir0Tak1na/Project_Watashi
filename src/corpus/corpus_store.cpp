#include "watashi/corpus.hpp"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "text_utf8.hpp"
#include "watashi/rules.hpp"

namespace watashi {
namespace {

namespace fs = std::filesystem;

double steady_seconds() {
    return std::chrono::duration<double>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

int layer_rank(Layer layer) {
    switch (layer) {
        case Layer::User: return 0;
        case Layer::Domain: return 1;
        case Layer::General: return 2;
    }
    return 9;
}

std::string layer_name(Layer layer) {
    switch (layer) {
        case Layer::User: return "user";
        case Layer::Domain: return "domain";
        case Layer::General: return "general";
    }
    return "";
}

std::string trim_ascii(std::string text) {
    const auto first = text.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) {
        return "";
    }
    const auto last = text.find_last_not_of(" \t\r\n");
    return text.substr(first, last - first + 1);
}

std::string domain_key(const std::string& value) {
    return text::lower_ascii(trim_ascii(value));
}

std::string near_key(const std::vector<std::string>& near) {
    std::string joined;
    for (std::size_t index = 0; index < near.size(); ++index) {
        if (index) {
            joined.push_back('\x1f');
        }
        joined += near[index];
    }
    return joined;
}

using Slot = std::tuple<std::string, std::u32string, std::string, std::string, std::string,
                        std::string>;

Slot slot_of(const Entry& entry) {
    std::string line;
    std::string window;
    std::string near;
    if (entry.conditions) {
        line = entry.conditions->line.value_or("");
        window = entry.conditions->window.value_or("");
        near = near_key(entry.conditions->near);
    }
    return Slot{CorpusStore::language_base(entry.lang), entry.normalized_source,
                domain_key(entry.domain), line, near, window};
}

bool better(const Entry& candidate, const Entry& incumbent) {
    const std::pair<int, int> candidate_rank{layer_rank(candidate.layer),
                                             -candidate.priority};
    const std::pair<int, int> incumbent_rank{layer_rank(incumbent.layer),
                                             -incumbent.priority};
    return candidate_rank < incumbent_rank;
}

int scene_rank(const Entry& entry, const Context& context) {
    if (context.scene.empty()) {
        return 0;
    }
    const std::string key = domain_key(entry.domain);
    if (key.empty()) {
        return 1;
    }
    return key == domain_key(context.scene) ? 0 : 2;
}

std::tuple<int, int, int, std::string, std::string, std::string> rank_of(
    const Entry& entry,
    const Context& context) {
    return {layer_rank(entry.layer), scene_rank(entry, context), -entry.priority,
            entry.origin, domain_key(entry.domain), entry.target};
}

bool conditions_hold(
    const Conditions& conditions,
    const Context& context,
    std::u32string_view text,
    std::size_t start,
    std::size_t length) {
    if (conditions.line_regex &&
        !std::regex_search(text::to_utf8(text), *conditions.line_regex)) {
        return false;
    }
    if (conditions.window) {
        // No window known means the condition cannot be checked, and an entry
        // that asked for one must not leak into every other scene.
        if (context.window.empty()) {
            return false;
        }
        if (!conditions.window_regex ||
            !std::regex_search(context.window, *conditions.window_regex)) {
            return false;
        }
    }
    if (!conditions.near.empty()) {
        std::u32string rest(text.substr(0, start));
        rest.push_back(U' ');
        rest += text.substr(start + length);
        const std::u32string haystack = text::lower_ascii(rest);
        bool found = false;
        for (const auto& word : conditions.near) {
            if (haystack.find(text::lower_ascii(text::to_u32(word))) != std::u32string::npos) {
                found = true;
                break;
            }
        }
        if (!found) {
            return false;
        }
    }
    return true;
}

const Entry* select_entry(
    const std::vector<const Entry*>& candidates,
    const Context& context,
    std::u32string_view text,
    std::size_t start,
    std::size_t length) {
    if (candidates.empty()) {
        return nullptr;
    }
    if (candidates.size() == 1) {
        const Entry* only = candidates.front();
        if (!only->conditions || conditions_hold(*only->conditions, context, text, start, length)) {
            return only;
        }
        return nullptr;
    }
    const Entry* best = nullptr;
    for (const Entry* entry : candidates) {
        if (entry->conditions &&
            !conditions_hold(*entry->conditions, context, text, start, length)) {
            continue;
        }
        if (best == nullptr || rank_of(*entry, context) < rank_of(*best, context)) {
            best = entry;
        }
    }
    return best;
}

nlohmann::json conflict_record(const Entry& loser, const Entry& winner, const std::string& why) {
    return nlohmann::json{
        {"source", loser.source},
        {"lang", loser.lang},
        {"domain", loser.domain},
        {"kept",
         nlohmann::json{{"target", winner.target},
                        {"origin", winner.origin},
                        {"layer", layer_name(winner.layer)},
                        {"priority", winner.priority}}},
        {"dropped",
         nlohmann::json{{"target", loser.target},
                        {"origin", loser.origin},
                        {"layer", layer_name(loser.layer)},
                        {"priority", loser.priority}}},
        {"why", why},
    };
}

std::optional<nlohmann::ordered_json> read_json(const std::string& path) {
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        return std::nullopt;
    }
    try {
        nlohmann::ordered_json data;
        file >> data;
        return data;
    } catch (const std::exception&) {
        return std::nullopt;
    }
}

std::string first_string(const nlohmann::ordered_json& value,
                         std::initializer_list<const char*> keys) {
    for (const char* key : keys) {
        const auto found = value.find(key);
        if (found != value.end() && found->is_string() &&
            !found->get<std::string>().empty()) {
            return found->get<std::string>();
        }
    }
    return "";
}

std::shared_ptr<const Conditions> build_conditions(
    const nlohmann::ordered_json& value,
    const std::string& origin) {
    if (!value.is_object()) {
        return nullptr;
    }
    auto conditions = std::make_shared<Conditions>();

    const auto compile = [&](const char* key) -> std::shared_ptr<const std::regex> {
        const auto found = value.find(key);
        if (found == value.end() || !found->is_string()) {
            return nullptr;
        }
        const std::string pattern = trim_ascii(found->get<std::string>());
        if (pattern.empty()) {
            std::cerr << "[translate] " << origin << ": " << key
                      << " needs a non-empty pattern, ignored\n";
            return nullptr;
        }
        try {
            return std::make_shared<const std::regex>(
                pattern, std::regex_constants::ECMAScript | std::regex_constants::icase);
        } catch (const std::regex_error& error) {
            std::cerr << "[translate] " << origin << ": " << key
                      << " is not a valid regex (" << error.what() << "), ignored\n";
            return nullptr;
        }
    };

    const auto line_found = value.find("when_line");
    if (line_found != value.end() && line_found->is_string()) {
        conditions->line = trim_ascii(line_found->get<std::string>());
        conditions->line_regex = compile("when_line");
        if (!conditions->line_regex) {
            conditions->line.reset();
        }
    }
    const auto window_found = value.find("when_window");
    if (window_found != value.end() && window_found->is_string()) {
        conditions->window = trim_ascii(window_found->get<std::string>());
        conditions->window_regex = compile("when_window");
        if (!conditions->window_regex) {
            conditions->window.reset();
        }
    }

    const auto near_found = value.find("when_near");
    const auto near_alias = value.find("when_nearby");
    const nlohmann::ordered_json* raw_near =
        near_found != value.end() ? &*near_found
                                  : (near_alias != value.end() ? &*near_alias : nullptr);
    if (raw_near != nullptr) {
        if (raw_near->is_string()) {
            std::stringstream stream(raw_near->get<std::string>());
            std::string item;
            while (std::getline(stream, item, ',')) {
                item = trim_ascii(item);
                if (!item.empty()) {
                    conditions->near.push_back(item);
                }
            }
        } else if (raw_near->is_array()) {
            for (const auto& item : *raw_near) {
                if (item.is_string()) {
                    const std::string trimmed = trim_ascii(item.get<std::string>());
                    if (!trimmed.empty()) {
                        conditions->near.push_back(trimmed);
                    }
                }
            }
        }
    }

    if (conditions->line_regex == nullptr && conditions->window_regex == nullptr &&
        conditions->near.empty()) {
        return nullptr;
    }
    return conditions;
}

struct Loaded {
    std::vector<std::unique_ptr<Entry>> entries;
    std::vector<nlohmann::json> conflicts;
    std::vector<std::string> suppressed;
    std::size_t hidden = 0;
};

void add_entries(
    const std::string& source_key,
    const nlohmann::ordered_json& value,
    Layer layer,
    const std::string& origin,
    const std::string& file_lang,
    const std::set<std::u32string>& suppressed,
    Loaded& loaded,
    std::map<Slot, std::size_t>& slot_index) {
    if (value.is_array()) {
        for (const auto& item : value) {
            add_entries(source_key, item, layer, origin, file_lang, suppressed, loaded,
                        slot_index);
        }
        return;
    }
    if (!value.is_string() && !value.is_object()) {
        return;
    }

    auto entry = std::make_unique<Entry>();
    entry->source = source_key;
    entry->normalized_source = CorpusStore::normalize(text::to_u32(source_key));
    entry->layer = layer;
    entry->origin = origin;
    entry->lang = file_lang;

    if (value.is_string()) {
        entry->target = value.get<std::string>();
    } else {
        const std::string target = first_string(value, {"target", "translation", "text"});
        if (target.empty()) {
            return;
        }
        entry->target = target;
        const std::string entry_lang = first_string(value, {"lang", "target_lang", "language"});
        if (!entry_lang.empty()) {
            entry->lang = entry_lang;
        }
        const auto priority = value.find("priority");
        if (priority != value.end() && priority->is_number_integer()) {
            entry->priority = priority->get<int>();
        }
        const auto pos = value.find("pos");
        if (pos != value.end() && pos->is_string()) {
            entry->pos = pos->get<std::string>();
        }
        const auto domain = value.find("domain");
        if (domain != value.end() && domain->is_string()) {
            entry->domain = domain->get<std::string>();
        }
        entry->conditions = build_conditions(value, origin);
    }

    if (layer != Layer::User && !suppressed.empty() &&
        suppressed.count(entry->normalized_source) > 0) {
        ++loaded.hidden;
        return;
    }

    const Slot slot = slot_of(*entry);
    const auto existing = slot_index.find(slot);
    if (existing == slot_index.end()) {
        slot_index.emplace(slot, loaded.entries.size());
        loaded.entries.push_back(std::move(entry));
        return;
    }

    Entry& incumbent = *loaded.entries[existing->second];
    if (better(*entry, incumbent)) {
        if (entry->target != incumbent.target) {
            loaded.conflicts.push_back(
                conflict_record(incumbent, *entry, "replaced by a higher layer"));
        }
        incumbent = *entry;
    } else if (entry->target != incumbent.target) {
        loaded.conflicts.push_back(
            conflict_record(*entry, incumbent, "ignored in favour of a higher layer"));
    }
}

std::string quoted(const std::string& text) {
    std::string out = "'";
    for (char value : text) {
        if (value == '\\' || value == '\'') {
            out.push_back('\\');
        }
        out.push_back(value);
    }
    out.push_back('\'');
    return out;
}

std::string format_confidence(double value) {
    char buffer[32];
    std::snprintf(buffer, sizeof(buffer), "%.2f", value);
    return buffer;
}

}  // namespace

// --------------------------------------------------------------------------- //
// data model helpers
// --------------------------------------------------------------------------- //

bool Conditions::empty() const {
    return !line.has_value() && !window.has_value() && near.empty();
}

bool Span::explained() const {
    return origin.rfind("corpus:", 0) == 0 || origin.rfind("rule:", 0) == 0 ||
           origin.rfind("correction:", 0) == 0;
}

double Outcome::coverage() const {
    std::size_t total = 0;
    std::size_t explained = 0;
    for (const auto& span : spans) {
        if (!text::has_non_space(text::to_u32(span.source))) {
            continue;
        }
        const auto length = text::to_u32(span.source).size();
        total += length;
        if (span.explained()) {
            explained += length;
        }
    }
    if (total == 0) {
        return 0.0;
    }
    return static_cast<double>(explained) / static_cast<double>(total);
}

double Outcome::confidence() const {
    if (spans.empty()) {
        return 0.0;
    }
    double total = 0.0;
    for (const auto& span : spans) {
        total += span.confidence;
    }
    return total / static_cast<double>(spans.size());
}

std::string Outcome::trace() const {
    std::string out;
    for (const auto& span : spans) {
        if (!text::has_non_space(text::to_u32(span.source))) {
            continue;
        }
        if (!out.empty()) {
            out += ' ';
        }
        const std::string tag = span.rule_id.empty() ? span.origin : span.rule_id;
        out += quoted(span.source) + "->" + quoted(span.target) + "[" + tag + " " +
               format_confidence(span.confidence) + "]";
    }
    return out;
}

// --------------------------------------------------------------------------- //
// language tags
// --------------------------------------------------------------------------- //

namespace {

std::optional<std::string> nllb_code(const std::string& tag) {
    std::string key = text::lower_ascii(trim_ascii(tag));
    std::replace(key.begin(), key.end(), '_', '-');
    static const std::map<std::string, std::string> kCodes = {
        {"zh", "zho_Hans"},  {"zh-cn", "zho_Hans"}, {"zh-hans", "zho_Hans"},
        {"zh-tw", "zho_Hant"}, {"zh-hant", "zho_Hant"}, {"en", "eng_Latn"},
        {"ja", "jpn_Jpan"},  {"ko", "kor_Hang"},   {"fr", "fra_Latn"},
        {"de", "deu_Latn"},  {"es", "spa_Latn"},   {"pt", "por_Latn"},
        {"it", "ita_Latn"},  {"ru", "rus_Cyrl"},   {"ar", "arb_Arab"},
        {"th", "tha_Thai"},  {"vi", "vie_Latn"},   {"id", "ind_Latn"},
    };
    const auto found = kCodes.find(key);
    if (found != kCodes.end()) {
        return found->second;
    }
    // An NLLB code passed straight through, e.g. "zho_Hans".
    const std::string raw = trim_ascii(tag);
    if (raw.size() == 8 && raw[3] == '_') {
        const bool letters_before = std::all_of(raw.begin(), raw.begin() + 3, [](char c) {
            return c >= 'a' && c <= 'z';
        });
        const bool upper = raw[4] >= 'A' && raw[4] <= 'Z';
        const bool lower_after = std::all_of(raw.begin() + 5, raw.end(), [](char c) {
            return c >= 'a' && c <= 'z';
        });
        if (letters_before && upper && lower_after) {
            return raw;
        }
    }
    return std::nullopt;
}

}  // namespace

std::string CorpusStore::language_base(const std::string& tag) {
    if (tag.empty()) {
        return "";
    }
    const auto code = nllb_code(tag);
    if (code) {
        return code->substr(0, 3);
    }
    std::string literal = text::lower_ascii(trim_ascii(tag));
    std::replace(literal.begin(), literal.end(), '_', '-');
    return literal;
}

bool CorpusStore::same_language(const std::string& left, const std::string& right) {
    const auto left_code = nllb_code(left);
    const auto right_code = nllb_code(right);
    if (!left_code || !right_code) {
        return false;
    }
    return left_code->substr(0, 3) == right_code->substr(0, 3);
}

std::u32string CorpusStore::normalize(std::u32string_view text) {
    return text::lower_ascii(text);
}

// --------------------------------------------------------------------------- //
// construction and loading
// --------------------------------------------------------------------------- //

CorpusStore::CorpusStore(Options options)
    : options_(std::move(options)),
      auto_reload_(options_.auto_reload),
      reload_interval_(std::max(0.0, options_.reload_interval_s)) {
    load();
}

CorpusStore::~CorpusStore() = default;

void CorpusStore::collect_files(
    const std::vector<std::string>& roots,
    const std::function<void(const fs::path&)>& visit) const {
    for (const auto& root : roots) {
        const fs::path path(root);
        std::error_code error;
        if (fs::is_regular_file(path, error) && path.extension() == ".json") {
            visit(path);
        } else if (fs::is_directory(path, error)) {
            std::vector<fs::path> files;
            for (fs::recursive_directory_iterator it(path, error), end; it != end;
                 it.increment(error)) {
                if (it->is_regular_file(error) && it->path().extension() == ".json") {
                    files.push_back(it->path());
                }
            }
            std::sort(files.begin(), files.end());
            for (const auto& file : files) {
                visit(file);
            }
        }
    }
}

void CorpusStore::load() {
    Loaded loaded;
    std::map<Slot, std::size_t> slot_index;
    std::set<std::u32string> suppressed;
    last_error_.clear();

    const std::pair<Layer, const std::vector<std::string>*> layers[] = {
        {Layer::User, &options_.user_dirs},
        {Layer::Domain, &options_.domain_dirs},
        {Layer::General, &options_.general_dirs},
    };

    for (const auto& [layer, roots] : layers) {
        collect_files(*roots, [&](const fs::path& path) {
            const std::string key = path.string();
            auto parsed = read_json(key);
            if (!parsed) {
                const auto cached = last_good_.find(key);
                if (cached != last_good_.end()) {
                    parsed = cached->second;
                    if (last_error_.empty()) {
                        last_error_ = key + ": invalid JSON, kept the last good contents";
                    }
                    std::cerr << "[translate] " << key
                              << ": invalid JSON, kept last good contents\n";
                } else {
                    if (last_error_.empty()) {
                        last_error_ = key + ": unreadable or invalid JSON";
                    }
                    std::cerr << "[translate] skipping " << key << "\n";
                    return;
                }
            } else {
                last_good_[key] = *parsed;
            }
            const nlohmann::ordered_json& data = *parsed;
            if (!data.is_object()) {
                return;
            }
            const auto entries_key = data.find("entries");
            const bool wrapped = entries_key != data.end() && entries_key->is_object();
            const nlohmann::ordered_json& body = wrapped ? *entries_key : data;
            std::string file_lang;
            if (wrapped) {
                file_lang = first_string(data, {"lang", "target_lang", "language"});
            }
            if (layer == Layer::User) {
                const auto raw_suppress = data.find("_suppress");
                if (raw_suppress != data.end() && raw_suppress->is_array()) {
                    for (const auto& item : *raw_suppress) {
                        if (item.is_string() && !trim_ascii(item.get<std::string>()).empty()) {
                            suppressed.insert(normalize(text::to_u32(item.get<std::string>())));
                        }
                    }
                }
            }
            const std::string origin = "corpus:" + path.stem().string();
            for (auto it = body.begin(); it != body.end(); ++it) {
                const std::string source_key = it.key();
                if (source_key.empty() || source_key.front() == '_') {
                    continue;
                }
                add_entries(source_key, it.value(), layer, origin, file_lang, suppressed,
                            loaded, slot_index);
            }
        });
    }

    std::string rule_error;
    std::unique_ptr<RuleSet> rules = RuleSet::from_files(options_.rule_files, &rule_error);
    if (!rule_error.empty() && last_error_.empty()) {
        last_error_ = rule_error;
    }

    // Views: one per declared language, plus the language-neutral one. The
    // merge keeps layer precedence, so an untagged user override still beats a
    // tagged shipped entry it was written to replace.
    std::map<std::u32string, std::vector<const Entry*>> untagged;
    std::map<std::string, std::map<std::u32string, std::vector<const Entry*>>> tagged;
    for (const auto& entry : loaded.entries) {
        const std::string base = language_base(entry->lang);
        if (base.empty()) {
            untagged[entry->normalized_source].push_back(entry.get());
        } else {
            tagged[base][entry->normalized_source].push_back(entry.get());
        }
    }

    const Context neutral;
    const auto freeze = [&neutral](std::map<std::u32string, std::vector<const Entry*>> merged) {
        View view;
        for (auto& [source, items] : merged) {
            std::stable_sort(items.begin(), items.end(), [&neutral](const Entry* left,
                                                                    const Entry* right) {
                return rank_of(*left, neutral) < rank_of(*right, neutral);
            });
            view.max_key = std::max(view.max_key, source.size());
        }
        view.entries = std::move(merged);
        return view;
    };

    std::map<std::string, View> views;
    views[""] = freeze(untagged);
    for (const auto& [base, extra] : tagged) {
        auto merged = untagged;
        for (const auto& [source, items] : extra) {
            auto& target = merged[source];
            target.insert(target.end(), items.begin(), items.end());
        }
        views[base] = freeze(std::move(merged));
    }

    entries_ = std::move(loaded.entries);
    conflicts_ = std::move(loaded.conflicts);
    hidden_count_ = loaded.hidden;
    suppressed_.clear();
    for (const auto& item : suppressed) {
        suppressed_.push_back(text::to_utf8(item));
    }
    std::sort(suppressed_.begin(), suppressed_.end());
    rules_ = std::move(rules);
    views_ = std::move(views);
    languages_.clear();
    for (const auto& [base, unused] : tagged) {
        (void)unused;
        languages_.push_back(base);
    }
    std::sort(languages_.begin(), languages_.end());
    mtimes_.clear();
    const auto snapshot = [&](const std::vector<std::string>& roots) {
        collect_files(roots, [&](const fs::path& path) {
            std::error_code error;
            const auto write = fs::last_write_time(path, error);
            const auto size = fs::file_size(path, error);
            if (!error) {
                mtimes_[path.string()] = {
                    std::chrono::duration<double>(write.time_since_epoch()).count(), size};
            }
        });
    };
    snapshot(options_.user_dirs);
    snapshot(options_.domain_dirs);
    snapshot(options_.general_dirs);
    for (const auto& file : options_.rule_files) {
        std::error_code error;
        const auto write = fs::last_write_time(file, error);
        const auto size = fs::file_size(file, error);
        if (!error) {
            mtimes_[file] = {std::chrono::duration<double>(write.time_since_epoch()).count(),
                             size};
        }
    }
    ++revision_;
}

bool CorpusStore::reload_if_changed(double now, bool force) {
    if (!auto_reload_ && !force) {
        return false;
    }
    const double current = now < 0.0 ? steady_seconds() : now;
    if (!force) {
        if (current - last_check_ < reload_interval_) {
            return false;
        }
        last_check_ = current;
    }
    std::map<std::string, std::pair<double, std::uintmax_t>> snapshot;
    const auto scan = [&snapshot](const fs::path& path) {
        std::error_code error;
        const auto write = fs::last_write_time(path, error);
        const auto size = fs::file_size(path, error);
        if (!error) {
            snapshot[path.string()] = {
                std::chrono::duration<double>(write.time_since_epoch()).count(), size};
        }
    };
    for (const auto* roots : {&options_.user_dirs, &options_.domain_dirs,
                              &options_.general_dirs}) {
        collect_files(*roots, scan);
    }
    for (const auto& file : options_.rule_files) {
        scan(fs::path(file));
    }
    if (snapshot == mtimes_) {
        return false;
    }
    load();
    ++reloads_;
    return true;
}

void CorpusStore::set_reload_interval_s(double value) {
    reload_interval_ = std::max(0.0, value);
    last_check_ = -1e300;  // a shorter interval must not wait out the longer one
}

// --------------------------------------------------------------------------- //
// lookup
// --------------------------------------------------------------------------- //

const CorpusStore::View& CorpusStore::view_for(const std::string& target_lang) const {
    const std::string base = language_base(target_lang);
    const auto found = views_.find(base);
    if (found != views_.end()) {
        return found->second;
    }
    return views_.at("");
}

const Entry* CorpusStore::pick(
    const std::vector<const Entry*>& candidates,
    const Context& context,
    std::u32string_view text,
    std::size_t start,
    std::size_t length) const {
    return select_entry(candidates, context, text, start, length);
}

const Entry* CorpusStore::lookup_exact(
    std::u32string_view term,
    const std::string& target_lang,
    const Context& context) const {
    if (term.empty()) {
        return nullptr;
    }
    const std::u32string normalized = normalize(term);
    Context local = context;
    if (!target_lang.empty()) {
        local.target_lang = target_lang;
    }
    if (!local.target_lang.empty()) {
        const View& view = view_for(local.target_lang);
        const auto found = view.entries.find(normalized);
        if (found == view.entries.end()) {
            return nullptr;
        }
        return pick(found->second, local, term, 0, term.size());
    }
    const View& neutral = views_.at("");
    const auto direct = neutral.entries.find(normalized);
    if (direct != neutral.entries.end()) {
        if (const Entry* hit = pick(direct->second, local, term, 0, term.size())) {
            return hit;
        }
    }
    for (const auto& base : languages_) {
        const View& view = view_for(base);
        const auto found = view.entries.find(normalized);
        if (found == view.entries.end()) {
            continue;
        }
        if (const Entry* hit = pick(found->second, local, term, 0, term.size())) {
            return hit;
        }
    }
    return nullptr;
}

// --------------------------------------------------------------------------- //
// translate
// --------------------------------------------------------------------------- //

Outcome CorpusStore::translate(
    std::string_view text_value,
    const std::string& target_lang,
    const Context& context) {
    const std::string source_text(text_value);
    const std::u32string characters = text::to_u32(source_text);
    if (!text::has_non_space(characters)) {
        return Outcome{source_text, source_text, {}, "corpus+rules"};
    }

    reload_if_changed();

    Context local = context;
    local.target_lang = target_lang;
    const View& view = view_for(target_lang);
    const std::u32string normalized = normalize(characters);

    std::vector<std::size_t> offsets(characters.size() + 1, source_text.size());
    {
        std::size_t byte = 0;
        std::size_t index = 0;
        for (std::size_t position = 0; position < source_text.size();) {
            const auto lead = static_cast<unsigned char>(source_text[position]);
            const std::size_t length = lead < 0x80 ? 1 : ((lead & 0xE0) == 0xC0 ? 2
                                                     : ((lead & 0xF0) == 0xE0 ? 3 : 4));
            if (index < offsets.size()) {
                offsets[index] = position;
            }
            position += length;
            ++index;
            (void)byte;
        }
        if (index < offsets.size()) {
            offsets[index] = source_text.size();
        }
    }
    const auto slice = [&](std::size_t start, std::size_t length) {
        const std::size_t begin = offsets[start];
        const std::size_t end = offsets[std::min(start + length, characters.size())];
        return source_text.substr(begin, end - begin);
    };

    std::vector<Span> spans;
    const std::size_t size = normalized.size();
    std::size_t index = 0;
    std::size_t pending_start = 0;

    const auto flush = [&](std::size_t end) {
        if (end <= pending_start) {
            return;
        }
        std::size_t position = pending_start;
        while (position < end) {
            const char32_t value = characters[position];
            const bool latin = text::is_ascii_letter(value);
            const bool cjk = text::is_cjk(value);
            if (!latin && !cjk) {
                std::size_t stop = position;
                while (stop < end && !text::is_ascii_letter(characters[stop]) &&
                       !text::is_cjk(characters[stop])) {
                    ++stop;
                }
                const std::string literal = slice(position, stop - position);
                spans.push_back(Span{literal, literal, "literal", 0.0, ""});
                position = stop;
                continue;
            }
            std::size_t stop = position + 1;
            if (latin) {
                while (stop < end &&
                       (text::is_ascii_letter(characters[stop]) ||
                        characters[stop] == U'\'' || characters[stop] == U'\u2019' ||
                        characters[stop] == U'-')) {
                    ++stop;
                }
            } else {
                while (stop < end && text::is_cjk(characters[stop])) {
                    ++stop;
                }
            }
            const std::u32string token = characters.substr(position, stop - position);
            const std::string token_text = slice(position, stop - position);
            std::optional<RuleHit> hit;
            if (rules_) {
                hit = rules_->apply(token, *this, local.target_lang);
            }
            if (hit) {
                spans.push_back(Span{token_text, hit->target,
                                     "rule:" + hit->kind, hit->confidence, hit->rule_id});
            } else {
                spans.push_back(Span{token_text, token_text, "literal", 0.0, ""});
            }
            position = stop;
        }
        pending_start = end;
    };

    while (index < size) {
        const Entry* hit = nullptr;
        std::size_t length = 0;
        const std::size_t upper = std::min(view.max_key, size - index);
        for (std::size_t candidate_length = upper; candidate_length > 0; --candidate_length) {
            const std::u32string candidate = normalized.substr(index, candidate_length);
            const auto found = view.entries.find(candidate);
            if (found == view.entries.end()) {
                continue;
            }
            const Entry* entry = pick(found->second, local, characters, index, candidate_length);
            if (entry == nullptr) {
                continue;
            }
            if (text::is_ascii_alnum(candidate.front()) && index > 0 &&
                text::is_word_char(characters[index - 1])) {
                continue;
            }
            if (text::is_ascii_alnum(candidate.back()) && index + candidate_length < size &&
                text::is_word_char(characters[index + candidate_length])) {
                continue;
            }
            hit = entry;
            length = candidate_length;
            break;
        }
        if (hit == nullptr) {
            ++index;
            continue;
        }
        flush(index);
        spans.push_back(Span{slice(index, length), hit->target, hit->origin,
                             hit->layer == Layer::User ? 1.0 : 0.95, ""});
        index += length;
        pending_start = index;
    }
    flush(size);

    std::string target_text;
    for (const auto& span : spans) {
        target_text += span.target;
    }
    std::string squeezed;
    for (std::size_t position = 0; position < target_text.size();) {
        if (target_text[position] == ' ' || target_text[position] == '\t') {
            std::size_t stop = position;
            while (stop < target_text.size() &&
                   (target_text[stop] == ' ' || target_text[stop] == '\t')) {
                ++stop;
            }
            // Python collapses only runs of two or more; a single tab survives.
            if (stop - position >= 2) {
                squeezed.push_back(' ');
            } else {
                squeezed.append(target_text, position, stop - position);
            }
            position = stop;
        } else {
            squeezed.push_back(target_text[position]);
            ++position;
        }
    }
    const auto first = squeezed.find_first_not_of(" \t\r\n\v\f");
    const auto last = squeezed.find_last_not_of(" \t\r\n\v\f");
    target_text = first == std::string::npos ? "" : squeezed.substr(first, last - first + 1);

    return Outcome{source_text, target_text, std::move(spans), "corpus+rules"};
}

// --------------------------------------------------------------------------- //
// introspection
// --------------------------------------------------------------------------- //

std::size_t CorpusStore::rule_count() const {
    return rules_ ? rules_->size() : 0;
}

std::vector<std::string> CorpusStore::rule_ids() const {
    return rules_ ? rules_->ids() : std::vector<std::string>{};
}

const RuleSet& CorpusStore::rules() const {
    static const RuleSet kEmpty;
    return rules_ ? *rules_ : kEmpty;
}

std::vector<std::string> CorpusStore::suppressed_sources() const {
    return suppressed_;
}

std::vector<std::string> CorpusStore::languages() const {
    return languages_;
}

}  // namespace watashi
