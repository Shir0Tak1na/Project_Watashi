// Rule inference for words the corpus does not cover.
//
// Four rule types, loaded from JSON data files exactly like the Python engine:
// ``affix`` (strip a known prefix/suffix, translate the stem), ``morpheme``
// (split an unknown word into known pieces), ``template`` (a naming-convention
// regex) and ``transliterate`` (the last-resort identity mapping). Each rule
// declares its own confidence, which is what lets the UI dim uncertain output.

#pragma once

#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json.hpp>

#include "watashi/corpus.hpp"

namespace watashi {

/// One rule that produced an answer.
struct RuleHit {
    std::string target;
    double confidence = 0.0;
    std::string rule_id;
    std::string kind;
};

class Rule {
public:
    virtual ~Rule() = default;

    [[nodiscard]] virtual std::string_view kind() const = 0;
    /// Returns nothing when the rule does not apply (or has no answer).
    [[nodiscard]] virtual std::optional<std::string> apply(
        std::u32string_view token,
        const CorpusStore& corpus,
        const std::string& target_lang) const = 0;

    [[nodiscard]] bool matches_language(const std::string& target_lang) const;
    [[nodiscard]] const std::string& id() const { return id_; }
    [[nodiscard]] int priority() const { return priority_; }
    [[nodiscard]] double confidence() const { return confidence_; }
    [[nodiscard]] bool enabled() const { return enabled_; }

protected:
    Rule(const nlohmann::json& spec, const std::string& origin, std::string_view kind);

    std::string id_;
    int priority_ = 100;
    double confidence_ = 0.4;
    std::optional<std::string> target_;
    bool enabled_ = true;
};

class AffixRule final : public Rule {
public:
    AffixRule(const nlohmann::json& spec, const std::string& origin);
    [[nodiscard]] std::string_view kind() const override { return "affix"; }
    [[nodiscard]] std::optional<std::string> apply(
        std::u32string_view token,
        const CorpusStore& corpus,
        const std::string& target_lang) const override;

private:
    // Keys are normalized source affixes, values the gloss to re-attach.
    std::vector<std::pair<std::u32string, std::string>> prefixes_;
    std::vector<std::pair<std::u32string, std::string>> suffixes_;
    std::size_t min_stem_ = 2;
};

class MorphemeRule final : public Rule {
public:
    MorphemeRule(const nlohmann::json& spec, const std::string& origin);
    [[nodiscard]] std::string_view kind() const override { return "morpheme"; }
    [[nodiscard]] std::optional<std::string> apply(
        std::u32string_view token,
        const CorpusStore& corpus,
        const std::string& target_lang) const override;

private:
    std::string separator_;
};

class TemplateRule final : public Rule {
public:
    TemplateRule(const nlohmann::json& spec, const std::string& origin);
    [[nodiscard]] std::string_view kind() const override { return "template"; }
    [[nodiscard]] std::optional<std::string> apply(
        std::u32string_view token,
        const CorpusStore& corpus,
        const std::string& target_lang) const override;
    [[nodiscard]] bool usable() const { return usable_; }

private:
    std::optional<std::wregex> regex_;
    std::vector<std::string> group_names_;
    std::string replacement_;
    bool usable_ = false;
};

class TransliterateRule final : public Rule {
public:
    TransliterateRule(const nlohmann::json& spec, const std::string& origin);
    [[nodiscard]] std::string_view kind() const override { return "transliterate"; }
    [[nodiscard]] std::optional<std::string> apply(
        std::u32string_view token,
        const CorpusStore& corpus,
        const std::string& target_lang) const override;
};

/// Every enabled rule from one or more rule files, sorted by priority.
class RuleSet {
public:
    static std::unique_ptr<RuleSet> from_files(
        const std::vector<std::string>& paths,
        std::string* error = nullptr);

    [[nodiscard]] std::optional<RuleHit> apply(
        std::u32string_view token,
        const CorpusStore& corpus,
        const std::string& target_lang) const;

    [[nodiscard]] std::size_t size() const { return rules_.size(); }
    [[nodiscard]] std::vector<std::string> ids() const;
    [[nodiscard]] const std::vector<std::unique_ptr<Rule>>& rules() const { return rules_; }

private:
    std::vector<std::unique_ptr<Rule>> rules_;
};

}  // namespace watashi
