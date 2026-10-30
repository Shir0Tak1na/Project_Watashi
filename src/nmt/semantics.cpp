// Pure language and term-protection semantics, copied from local_nmt.py.
//
// Nothing here needs a model, a tokenizer or a clock: the no-model tests drive
// this directly, and the differential test checks the same functions the CLI
// uses in production. The port is behavioural, not line-by-line, but every
// string and threshold that a reference case can observe is kept identical.

#include "watashi/nmt.hpp"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <map>
#include <set>
#include <string>
#include <vector>

#include "corpus/text_utf8.hpp"

namespace watashi::nmt_detail {
namespace {

using text::to_u32;

constexpr const char* kPlaceholderOpen = "\u2264";  // <=
constexpr const char* kPlaceholderClose = "\u2265";  // >=

bool is_ascii_space(char32_t value) {
    return value == U' ' || value == U'\t' || value == U'\r' || value == U'\n' ||
           value == U'\f' || value == U'\v';
}

bool is_space(char32_t value) {
    return is_ascii_space(value) || value == U'\u3000' || value == 0x0085 || value == 0x00A0;
}

std::string strip(std::string_view text) {
    const auto codepoints = to_u32(text);
    std::size_t first = 0;
    while (first < codepoints.size() && is_space(codepoints[first])) {
        ++first;
    }
    std::size_t last = codepoints.size();
    while (last > first && is_space(codepoints[last - 1])) {
        --last;
    }
    // keep the original bytes between the trimmed code points
    std::u32string trimmed(codepoints.begin() + static_cast<std::ptrdiff_t>(first),
                           codepoints.begin() + static_cast<std::ptrdiff_t>(last));
    return text::to_utf8(trimmed);
}

bool is_unicode_letter(char32_t value) {
    if (text::is_ascii_letter(value)) {
        return true;
    }
    return (value >= 0x00C0 && value <= 0x024F) ||  // Latin-1 supplement / extended
           (value >= 0x0370 && value <= 0x03FF) ||  // Greek
           (value >= 0x0400 && value <= 0x052F) ||  // Cyrillic
           (value >= 0x0530 && value <= 0x058F) ||  // Armenian
           (value >= 0x0590 && value <= 0x05FF) ||  // Hebrew
           (value >= 0x0600 && value <= 0x06FF) ||  // Arabic
           (value >= 0x0900 && value <= 0x097F) ||  // Devanagari
           (value >= 0x0E00 && value <= 0x0E7F) ||  // Thai
           (value >= 0x3040 && value <= 0x30FF) ||  // kana
           (value >= 0x3400 && value <= 0x4DBF) ||  // Han extension A
           (value >= 0x4E00 && value <= 0x9FFF) ||  // Han
           (value >= 0xAC00 && value <= 0xD7AF) ||  // Hangul
           (value >= 0xFF66 && value <= 0xFF9F);    // halfwidth kana
}

bool has_latin_letter(std::string_view text) {
    for (char32_t value : to_u32(text)) {
        if (text::is_ascii_letter(value)) {
            return true;
        }
    }
    return false;
}

bool is_cjk_or_punctuation(char32_t value) {
    return (value >= 0x3400 && value <= 0x4DBF) || (value >= 0x4E00 && value <= 0x9FFF) ||
           (value >= 0x3040 && value <= 0x30FF) || (value >= 0xAC00 && value <= 0xD7AF) ||
           (value >= 0x3000 && value <= 0x303F) || (value >= 0xFF00 && value <= 0xFFEF);
}

bool is_unspaced_base(const std::string& code) {
    const auto base = code.substr(0, code.find('_'));
    return base == "zho" || base == "jpn" || base == "kor" || base == "tha";
}

/// True when a lowercase Latin run of two or more letters remains.
bool latin_residue(std::string_view text) {
    const auto codepoints = to_u32(text);
    std::vector<char32_t> run;
    for (char32_t value : codepoints) {
        if (text::is_ascii_letter(value)) {
            run.push_back(value);
            continue;
        }
        if (!run.empty()) {
            if (run.size() >= 2) {
                bool all_upper = true;
                for (char32_t item : run) {
                    if (item >= U'a' && item <= U'z') {
                        all_upper = false;
                        break;
                    }
                }
                if (!all_upper) {
                    return true;
                }
            }
            run.clear();
        }
    }
    if (!run.empty() && run.size() >= 2) {
        for (char32_t item : run) {
            if (item >= U'a' && item <= U'z') {
                return true;
            }
        }
    }
    return false;
}

std::string compact_whitespace(std::string_view text) {
    std::u32string out;
    for (char32_t value : to_u32(text)) {
        if (!is_space(value)) {
            out.push_back(value);
        }
    }
    return text::to_utf8(out);
}

bool placeholder_at(std::string_view text, std::size_t index, std::size_t& length,
                    std::string& placeholder) {
    const std::string open = kPlaceholderOpen;
    const std::string close = kPlaceholderClose;
    if (index + open.size() + close.size() + 1 > text.size()) {
        return false;
    }
    if (text.compare(index, open.size(), open) != 0) {
        return false;
    }
    std::size_t cursor = index + open.size();
    std::size_t digits = 0;
    while (cursor < text.size() && text[cursor] >= '0' && text[cursor] <= '9') {
        ++cursor;
        ++digits;
    }
    if (digits == 0 || cursor >= text.size()) {
        return false;
    }
    if (text.compare(cursor, close.size(), close) != 0) {
        return false;
    }
    length = cursor + close.size() - index;
    placeholder = std::string(text.substr(index, length));
    return true;
}

}  // namespace

std::size_t codepoint_count(std::string_view text) {
    return to_u32(text).size();
}

bool is_blank(std::string_view text) {
    for (char32_t value : to_u32(text)) {
        if (!is_space(value)) {
            return false;
        }
    }
    return true;
}

std::string to_nllb_code(std::string_view tag) {
    const std::string trimmed = strip(tag);
    std::string normalized = text::lower_ascii(trimmed);
    std::replace(normalized.begin(), normalized.end(), '_', '-');

    static const std::vector<std::pair<std::string, std::string>> kCodes = {
        {"zh", "zho_Hans"},     {"zh-cn", "zho_Hans"}, {"zh-hans", "zho_Hans"},
        {"zh-tw", "zho_Hant"},  {"zh-hant", "zho_Hant"}, {"en", "eng_Latn"},
        {"ja", "jpn_Jpan"},     {"ko", "kor_Hang"},    {"fr", "fra_Latn"},
        {"de", "deu_Latn"},     {"es", "spa_Latn"},    {"pt", "por_Latn"},
        {"it", "ita_Latn"},     {"ru", "rus_Cyrl"},    {"ar", "arb_Arab"},
        {"th", "tha_Thai"},     {"vi", "vie_Latn"},    {"id", "ind_Latn"},
    };
    for (const auto& [code, value] : kCodes) {
        if (normalized == code) {
            return value;
        }
    }
    // NLLB codes pass through: exactly `[a-z]{3}_[A-Z][a-z]{3}` on the raw tag.
    const auto lower = [](char value) { return value >= 'a' && value <= 'z'; };
    const std::string raw(tag);
    const bool nllb_shape = raw.size() == 8 && raw[3] == '_' && lower(raw[0]) &&
                            lower(raw[1]) && lower(raw[2]) && raw[4] >= 'A' &&
                            raw[4] <= 'Z' && lower(raw[5]) && lower(raw[6]) && lower(raw[7]);
    if (nllb_shape) {
        return raw;
    }
    throw std::invalid_argument("no NLLB code known for language " + normalized);
}

std::string decisive_language(std::string_view text) {
    const std::string trimmed = strip(text);
    if (trimmed.empty()) {
        return "";
    }
    const auto codepoints = to_u32(text);
    const double length = static_cast<double>(std::max<std::size_t>(1, codepoints.size()));
    const auto share = [&codepoints, length](bool (*test)(char32_t)) {
        double hits = 0.0;
        for (char32_t value : codepoints) {
            if (test(value)) {
                hits += 1.0;
            }
        }
        return hits / length;
    };
    const double kana = share([](char32_t v) { return v >= 0x3040 && v <= 0x30FF; });
    if (kana > 0.0 && kana >= 0.10) {
        return "jpn_Jpan";
    }
    const double hangul = share([](char32_t v) { return v >= 0xAC00 && v <= 0xD7AF; });
    if (hangul > 0.0 && hangul >= 0.10) {
        return "kor_Hang";
    }
    const double thai = share([](char32_t v) { return v >= 0x0E00 && v <= 0x0E7F; });
    if (thai > 0.0 && thai >= 0.10) {
        return "tha_Thai";
    }
    const double arabic = share([](char32_t v) { return v >= 0x0600 && v <= 0x06FF; });
    if (arabic > 0.0 && arabic >= 0.10) {
        return "arb_Arab";
    }
    const double cyrillic = share([](char32_t v) { return v >= 0x0400 && v <= 0x04FF; });
    if (cyrillic > 0.0 && cyrillic >= 0.10) {
        return "rus_Cyrl";
    }
    const double han =
        share([](char32_t v) { return (v >= 0x3400 && v <= 0x4DBF) || (v >= 0x4E00 && v <= 0x9FFF); });
    if (han > 0.0 && han >= 0.15) {
        return "zho_Hans";
    }
    return "";
}

std::string detect_language(std::string_view text, std::string_view fallback) {
    const std::string decisive = decisive_language(text);
    if (!decisive.empty()) {
        return decisive;
    }
    if (has_latin_letter(text)) {
        return "eng_Latn";
    }
    return std::string(fallback);
}

bool has_translatable_content(std::string_view text) {
    for (char32_t value : to_u32(text)) {
        if (is_unicode_letter(value)) {
            return true;
        }
    }
    return false;
}

bool matches_target(std::string_view text, std::string_view target_lang) {
    if (strip(text).empty()) {
        return false;
    }
    std::string target;
    try {
        target = to_nllb_code(target_lang);
    } catch (const std::exception&) {
        return false;
    }
    const std::string detected = detect_language(text, "");
    if (detected.empty()) {
        return false;
    }
    const std::string left = detected.substr(0, detected.find('_'));
    const std::string right = target.substr(0, target.find('_'));
    return left == right;
}

bool is_degenerate(std::string_view text, int max_run) {
    const std::u32string codepoints = to_u32(text);
    const std::size_t length = codepoints.size();
    if (length < 12) {
        return false;
    }
    std::map<char32_t, std::size_t> counts;
    for (char32_t value : codepoints) {
        counts[value] += 1;
    }
    std::size_t maximum = 0;
    for (const auto& [value, count] : counts) {
        (void)value;
        maximum = std::max(maximum, count);
    }
    if (static_cast<double>(maximum) / static_cast<double>(length) > 0.5) {
        return true;
    }
    for (std::size_t size = 1; size <= 3; ++size) {
        if (length <= size * static_cast<std::size_t>(max_run)) {
            continue;
        }
        bool repeats = true;
        for (std::size_t index = 0; index < size * static_cast<std::size_t>(max_run); ++index) {
            if (codepoints[index] != codepoints[index % size]) {
                repeats = false;
                break;
            }
        }
        if (repeats) {
            return true;
        }
    }
    return false;
}

std::string tidy_cjk_spacing(std::string_view text) {
    const auto codepoints = to_u32(text);
    std::u32string out;
    out.reserve(codepoints.size());
    std::size_t index = 0;
    while (index < codepoints.size()) {
        const char32_t value = codepoints[index];
        if (value != U' ' && value != U'\t') {
            out.push_back(value);
            ++index;
            continue;
        }
        std::size_t end = index;
        while (end < codepoints.size() &&
               (codepoints[end] == U' ' || codepoints[end] == U'\t')) {
            ++end;
        }
        std::size_t next = end;
        const bool left_ok = !out.empty() && is_cjk_or_punctuation(out.back());
        const bool right_ok = next < codepoints.size() && is_cjk_or_punctuation(codepoints[next]);
        if (!(left_ok && right_ok)) {
            out.insert(out.end(), codepoints.begin() + static_cast<std::ptrdiff_t>(index),
                       codepoints.begin() + static_cast<std::ptrdiff_t>(end));
        }
        index = end;
    }
    return text::to_utf8(out);
}

ProtectedText protect_terms(std::string_view text, const Outcome& outcome, std::string_view scheme,
                            double min_confidence) {
    ProtectedText result;
    result.pattern = std::string(scheme);
    if (outcome.spans.empty()) {
        result.masked = std::string(text);
        return result;
    }
    std::string masked;
    for (const auto& span : outcome.spans) {
        if (span.source.empty()) {
            continue;
        }
        const std::string source_trimmed = strip(span.source);
        const std::string target_trimmed = strip(span.target);
        const bool authoritative = span.explained() && !source_trimmed.empty() &&
                                   span.confidence >= min_confidence &&
                                   target_trimmed != source_trimmed;
        if (authoritative) {
            const std::string placeholder =
                "\u2264" + std::to_string(result.terms.size()) + "\u2265";
            result.terms.emplace_back(placeholder, span.target);
            masked += placeholder;
        } else {
            masked += span.source;
        }
    }
    result.masked = masked;
    return result;
}

std::pair<std::string, std::vector<std::string>> restore_terms(
    std::string_view text, const ProtectedText& protected_text) {
    std::map<std::string, std::string> mapping;
    for (const auto& [placeholder, target] : protected_text.terms) {
        mapping[placeholder] = target;
    }
    std::string restored;
    std::set<std::string> found;
    std::size_t index = 0;
    while (index < text.size()) {
        std::size_t length = 0;
        std::string placeholder;
        if (placeholder_at(text, index, length, placeholder)) {
            found.insert(placeholder);
            auto it = mapping.find(placeholder);
            restored += it != mapping.end() ? it->second : placeholder;
            index += length;
            continue;
        }
        restored.push_back(text[index]);
        ++index;
    }
    std::vector<std::string> lost;
    for (const auto& [placeholder, target] : protected_text.terms) {
        (void)target;
        if (found.find(placeholder) == found.end()) {
            lost.push_back(placeholder);
        }
    }
    return {restored, lost};
}

int residual_word_count(std::string_view masked, std::string_view scheme) {
    (void)scheme;  // only the default angle-bracket form exists in this port
    std::string residual;
    std::size_t index = 0;
    while (index < masked.size()) {
        std::size_t length = 0;
        std::string placeholder;
        if (placeholder_at(masked, index, length, placeholder)) {
            residual.push_back(' ');
            index += length;
            continue;
        }
        residual.push_back(masked[index]);
        ++index;
    }
    int words = 0;
    std::u32string current;
    const auto flush = [&words, &current]() {
        if (!current.empty()) {
            for (char32_t value : current) {
                if (text::is_word_char(value)) {
                    ++words;
                    break;
                }
            }
            current.clear();
        }
    };
    for (char32_t value : to_u32(residual)) {
        if (is_space(value)) {
            flush();
        } else {
            current.push_back(value);
        }
    }
    flush();
    return words;
}

std::vector<std::string> term_targets(const Outcome& outcome, double min_confidence) {
    std::vector<std::string> targets;
    for (const auto& span : outcome.spans) {
        if (!span.explained()) {
            continue;
        }
        const std::string source_trimmed = strip(span.source);
        const std::string target_trimmed = strip(span.target);
        if (source_trimmed.empty() || target_trimmed.empty() ||
            target_trimmed == source_trimmed) {
            continue;
        }
        if (span.confidence < min_confidence) {
            continue;
        }
        targets.push_back(target_trimmed);
    }
    return targets;
}

std::vector<std::string> missing_terms(std::string_view text,
                                       const std::vector<std::string>& targets) {
    const std::string compact = compact_whitespace(text);
    std::vector<std::string> missing;
    for (const auto& target : targets) {
        const std::string term = compact_whitespace(target);
        if (compact.find(term) == std::string::npos) {
            missing.push_back(target);
        }
    }
    return missing;
}

bool corpus_is_complete(const Outcome& outcome, std::string_view target_lang) {
    const std::string target = strip(outcome.target_text);
    if (target.empty()) {
        return false;
    }
    if (!has_translatable_content(target)) {
        return true;
    }
    if (!matches_target(target, target_lang)) {
        return false;
    }
    std::string code;
    try {
        code = to_nllb_code(target_lang);
    } catch (const std::exception&) {
        return true;
    }
    if (is_unspaced_base(code)) {
        return !latin_residue(target);
    }
    return true;
}

bool usable_model_text(std::string_view text, std::string_view target_lang) {
    return !strip(text).empty() && !is_degenerate(text) && matches_target(text, target_lang);
}

std::vector<SourceGroup> group_by_source(const std::vector<std::string>& texts,
                                         std::string_view source_lang) {
    std::vector<SourceGroup> groups;
    std::map<std::string, std::size_t> positions;
    const std::string declared = strip(source_lang);
    const bool use_declared =
        !declared.empty() && text::lower_ascii(declared) != "auto";
    for (std::size_t index = 0; index < texts.size(); ++index) {
        std::string code;
        if (use_declared) {
            try {
                code = to_nllb_code(declared);
            } catch (const std::exception&) {
                code = detect_language(texts[index]);
            }
        } else {
            code = detect_language(texts[index]);
        }
        auto it = positions.find(code);
        if (it == positions.end()) {
            positions[code] = groups.size();
            groups.push_back(SourceGroup{code, {}});
            it = positions.find(code);
        }
        groups[it->second].indices.push_back(index);
    }
    return groups;
}

}  // namespace watashi::nmt_detail
