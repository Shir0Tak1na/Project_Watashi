#include "watashi/rules.hpp"

#include <algorithm>
#include <fstream>
#include <iostream>
#include <regex>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "text_utf8.hpp"

namespace watashi {
namespace {

std::optional<nlohmann::json> read_json(const std::string& path, std::string* error) {
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        if (error) {
            *error = "cannot read " + path;
        }
        return std::nullopt;
    }
    try {
        nlohmann::json data;
        file >> data;
        return data;
    } catch (const std::exception& failure) {
        if (error) {
            *error = path + ": " + failure.what();
        }
        return std::nullopt;
    }
}

std::string file_stem(const std::string& path) {
    const auto slash = path.find_last_of("/\\");
    std::string name = slash == std::string::npos ? path : path.substr(slash + 1);
    const auto dot = name.find_last_of('.');
    return dot == std::string::npos ? name : name.substr(0, dot);
}

/// Parse Python's ``(?P<name>`` groups into plain groups, remembering the order.
/// std::regex has no group-by-name API, and the rule files are written for
/// Python's syntax, so translating the pattern is the least surprising option.
bool translate_pattern(
    const std::string& pattern,
    std::string& transformed,
    std::vector<std::string>& names) {
    transformed.clear();
    names.clear();
    for (std::size_t index = 0; index < pattern.size();) {
        if (pattern.compare(index, 4, "(?P<") == 0) {
            const auto close = pattern.find('>', index + 4);
            if (close == std::string::npos) {
                return false;
            }
            names.push_back(pattern.substr(index + 4, close - (index + 4)));
            transformed += '(';
            index = close + 1;
        } else {
            transformed.push_back(pattern[index]);
            ++index;
        }
    }
    return true;
}

std::optional<std::string> apply_replacement(
    const std::string& replacement,
    const std::vector<std::pair<std::string, std::string>>& values) {
    std::string out;
    for (std::size_t index = 0; index < replacement.size();) {
        if (replacement[index] == '{') {
            const auto close = replacement.find('}', index + 1);
            if (close == std::string::npos) {
                return std::nullopt;
            }
            const std::string name = replacement.substr(index + 1, close - (index + 1));
            const auto found = std::find_if(
                values.begin(), values.end(),
                [&name](const auto& item) { return item.first == name; });
            if (found == values.end()) {
                return std::nullopt;  // Python's str.format would raise KeyError
            }
            out += found->second;
            index = close + 1;
        } else if (replacement[index] == '}' && index + 1 < replacement.size() &&
                   replacement[index + 1] == '}') {
            out.push_back('}');
            index += 2;
        } else {
            out.push_back(replacement[index]);
            ++index;
        }
    }
    return out;
}

}  // namespace

Rule::Rule(const nlohmann::json& spec, const std::string& origin, std::string_view kind) {
    id_ = spec.value("id", std::string());
    if (id_.empty()) {
        id_ = std::string(kind) + "-" + origin;
    }
    enabled_ = spec.value("enabled", true);
    priority_ = spec.value("priority", 100);
    confidence_ = spec.value("confidence", 0.4);
    if (spec.contains("target") && spec["target"].is_string() &&
        !spec["target"].get<std::string>().empty()) {
        target_ = spec["target"].get<std::string>();
    }
}

bool Rule::matches_language(const std::string& target_lang) const {
    if (!target_) {
        return true;
    }
    return CorpusStore::same_language(*target_, target_lang);
}

AffixRule::AffixRule(const nlohmann::json& spec, const std::string& origin)
    : Rule(spec, origin, "affix") {
    min_stem_ = static_cast<std::size_t>(std::max(0, spec.value("min_stem", 2)));
    const auto load = [this](const nlohmann::json& object,
                             std::vector<std::pair<std::u32string, std::string>>& into) {
        if (!object.is_object()) {
            return;
        }
        for (auto it = object.begin(); it != object.end(); ++it) {
            if (!it.value().is_string()) {
                continue;
            }
            into.emplace_back(CorpusStore::normalize(text::to_u32(it.key())),
                              it.value().get<std::string>());
        }
        std::stable_sort(into.begin(), into.end(), [](const auto& left, const auto& right) {
            return left.first.size() > right.first.size();
        });
    };
    load(spec.value("prefixes", nlohmann::json::object()), prefixes_);
    load(spec.value("suffixes", nlohmann::json::object()), suffixes_);
}

std::optional<std::string> AffixRule::apply(
    std::u32string_view token,
    const CorpusStore& corpus,
    const std::string& target_lang) const {
    const std::u32string low = CorpusStore::normalize(token);

    for (const auto& [affix, gloss] : prefixes_) {
        if (low.size() < affix.size() || low.compare(0, affix.size(), affix) != 0) {
            continue;
        }
        const std::u32string stem = std::u32string(token.substr(affix.size()));
        if (stem.size() < min_stem_) {
            continue;
        }
        const Entry* hit = corpus.lookup_exact(stem, target_lang);
        if (hit == nullptr) {
            continue;
        }
        return gloss + hit->target;
    }

    for (const auto& [affix, gloss] : suffixes_) {
        if (low.size() < affix.size() ||
            low.compare(low.size() - affix.size(), affix.size(), affix) != 0) {
            continue;
        }
        const std::u32string stem = std::u32string(token.substr(0, token.size() - affix.size()));
        if (stem.size() < min_stem_) {
            continue;
        }
        const Entry* hit = corpus.lookup_exact(stem, target_lang);
        if (hit == nullptr) {
            continue;
        }
        return hit->target + gloss;
    }
    return std::nullopt;
}

MorphemeRule::MorphemeRule(const nlohmann::json& spec, const std::string& origin)
    : Rule(spec, origin, "morpheme"), separator_(spec.value("separator", "")) {}

std::optional<std::string> MorphemeRule::apply(
    std::u32string_view token,
    const CorpusStore& corpus,
    const std::string& target_lang) const {
    const std::u32string low = CorpusStore::normalize(token);
    std::vector<std::string> pieces;
    std::size_t index = 0;
    while (index < low.size()) {
        const Entry* hit = nullptr;
        std::size_t end = low.size();
        for (; end > index; --end) {
            hit = corpus.lookup_exact(low.substr(index, end - index), target_lang);
            if (hit != nullptr) {
                break;
            }
        }
        if (hit == nullptr) {
            return std::nullopt;
        }
        pieces.push_back(hit->target);
        index = end;
    }
    if (pieces.size() < 2) {
        return std::nullopt;
    }
    std::string out;
    for (std::size_t piece = 0; piece < pieces.size(); ++piece) {
        if (piece != 0) {
            out += separator_;
        }
        out += pieces[piece];
    }
    return out;
}

TemplateRule::TemplateRule(const nlohmann::json& spec, const std::string& origin)
    : Rule(spec, origin, "template") {
    replacement_ = spec.value("replacement", "");
    if (!spec.contains("pattern") || !spec["pattern"].is_string()) {
        enabled_ = false;
        return;
    }
    std::string transformed;
    if (!translate_pattern(spec["pattern"].get<std::string>(), transformed, group_names_)) {
        enabled_ = false;
        return;
    }
    try {
        regex_ = std::wregex(text::to_wide(text::to_u32(transformed)),
                             std::regex_constants::ECMAScript);
        usable_ = true;
    } catch (const std::regex_error& error) {
        std::cerr << "[translate] bad regex in rule " << id_ << ": " << error.what() << "\n";
        enabled_ = false;
    }
}

std::optional<std::string> TemplateRule::apply(
    std::u32string_view token,
    const CorpusStore& corpus,
    const std::string& target_lang) const {
    if (!usable_ || !regex_) {
        return std::nullopt;
    }
    const std::wstring wide_token = text::to_wide(token);
    std::match_results<std::wstring::const_iterator> match;
    if (!std::regex_match(wide_token.begin(), wide_token.end(), match, *regex_)) {
        return std::nullopt;
    }
    if (group_names_.empty()) {
        if (replacement_.empty()) {
            return std::nullopt;
        }
        return replacement_;
    }

    std::vector<std::pair<std::string, std::string>> values;
    for (std::size_t index = 0; index < group_names_.size(); ++index) {
        if (index + 1 >= match.size() || !match[index + 1].matched) {
            continue;
        }
        const std::wstring captured(match[index + 1].first, match[index + 1].second);
        const std::u32string value = text::from_wide(captured);
        const Entry* hit = corpus.lookup_exact(value, target_lang);
        values.emplace_back(group_names_[index],
                            hit != nullptr ? hit->target : text::to_utf8(value));
    }
    return apply_replacement(replacement_, values);
}

TransliterateRule::TransliterateRule(const nlohmann::json& spec, const std::string& origin)
    : Rule(spec, origin, "transliterate") {}

std::optional<std::string> TransliterateRule::apply(
    std::u32string_view token,
    const CorpusStore& corpus,
    const std::string& target_lang) const {
    (void)corpus;
    (void)target_lang;
    if (!text::has_non_space(token)) {
        return std::nullopt;
    }
    return text::to_utf8(token);
}

std::unique_ptr<RuleSet> RuleSet::from_files(
    const std::vector<std::string>& paths,
    std::string* error) {
    auto set = std::make_unique<RuleSet>();
    for (const auto& path : paths) {
        const auto data = read_json(path, error);
        if (!data || !data->is_object()) {
            continue;
        }
        const auto rules = data->find("rules");
        if (rules == data->end() || !rules->is_array()) {
            continue;
        }
        for (const auto& spec : *rules) {
            if (!spec.is_object()) {
                continue;
            }
            const std::string type = text::lower_ascii(spec.value("type", ""));
            std::unique_ptr<Rule> rule;
            try {
                if (type == "affix") {
                    rule = std::make_unique<AffixRule>(spec, file_stem(path));
                } else if (type == "morpheme") {
                    rule = std::make_unique<MorphemeRule>(spec, file_stem(path));
                } else if (type == "template") {
                    rule = std::make_unique<TemplateRule>(spec, file_stem(path));
                } else if (type == "transliterate") {
                    rule = std::make_unique<TransliterateRule>(spec, file_stem(path));
                } else {
                    std::cerr << "[translate] unknown rule type " << spec.value("type", "")
                              << ", skipped\n";
                    continue;
                }
            } catch (const std::exception& failure) {
                std::cerr << "[translate] failed to build rule: " << failure.what() << "\n";
                continue;
            }
            if (rule->enabled()) {
                set->rules_.push_back(std::move(rule));
            }
        }
    }
    std::stable_sort(
        set->rules_.begin(), set->rules_.end(),
        [](const auto& left, const auto& right) {
            return left->priority() < right->priority();
        });
    return set;
}

std::optional<RuleHit> RuleSet::apply(
    std::u32string_view token,
    const CorpusStore& corpus,
    const std::string& target_lang) const {
    for (const auto& rule : rules_) {
        if (!rule->matches_language(target_lang)) {
            continue;
        }
        std::optional<std::string> result;
        try {
            result = rule->apply(token, corpus, target_lang);
        } catch (const std::exception&) {
            continue;  // one bad rule must not break a frame
        }
        if (result && !result->empty()) {
            return RuleHit{*result, rule->confidence(), rule->id(),
                           std::string(rule->kind())};
        }
    }
    return std::nullopt;
}

std::vector<std::string> RuleSet::ids() const {
    std::vector<std::string> out;
    out.reserve(rules_.size());
    for (const auto& rule : rules_) {
        out.push_back(rule->id());
    }
    return out;
}

}  // namespace watashi
