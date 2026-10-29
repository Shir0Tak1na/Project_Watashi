// UTF-8 <-> UTF-32 helpers shared by the corpus and the rules.
//
// The scanner counts characters, not bytes: Python slices code points, and a
// span that ends inside a CJK character would produce a mangled subtitle. Only
// ASCII case folding is applied, matching the Python ``normalize`` which is
// deliberately length preserving.

#pragma once

#include <cstdint>
#include <string>
#include <string_view>

namespace watashi::text {

inline std::u32string to_u32(std::string_view text) {
    std::u32string out;
    out.reserve(text.size());
    for (std::size_t index = 0; index < text.size();) {
        const auto lead = static_cast<unsigned char>(text[index]);
        char32_t codepoint = 0;
        std::size_t length = 1;
        if (lead < 0x80) {
            codepoint = lead;
        } else if ((lead & 0xE0) == 0xC0) {
            codepoint = lead & 0x1F;
            length = 2;
        } else if ((lead & 0xF0) == 0xE0) {
            codepoint = lead & 0x0F;
            length = 3;
        } else if ((lead & 0xF8) == 0xF0) {
            codepoint = lead & 0x07;
            length = 4;
        } else {
            codepoint = 0xFFFD;
        }
        for (std::size_t offset = 1; offset < length && index + offset < text.size(); ++offset) {
            const auto continuation = static_cast<unsigned char>(text[index + offset]);
            if ((continuation & 0xC0) != 0x80) {
                codepoint = 0xFFFD;
                break;
            }
            codepoint = (codepoint << 6) | (continuation & 0x3F);
        }
        out.push_back(codepoint);
        index += length;
    }
    return out;
}

inline std::string to_utf8(std::u32string_view text) {
    std::string out;
    out.reserve(text.size());
    for (char32_t codepoint : text) {
        if (codepoint <= 0x7F) {
            out.push_back(static_cast<char>(codepoint));
        } else if (codepoint <= 0x7FF) {
            out.push_back(static_cast<char>(0xC0 | (codepoint >> 6)));
            out.push_back(static_cast<char>(0x80 | (codepoint & 0x3F)));
        } else if (codepoint <= 0xFFFF) {
            out.push_back(static_cast<char>(0xE0 | (codepoint >> 12)));
            out.push_back(static_cast<char>(0x80 | ((codepoint >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (codepoint & 0x3F)));
        } else {
            out.push_back(static_cast<char>(0xF0 | (codepoint >> 18)));
            out.push_back(static_cast<char>(0x80 | ((codepoint >> 12) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | ((codepoint >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (codepoint & 0x3F)));
        }
    }
    return out;
}

inline bool is_ascii_letter(char32_t value) {
    return (value >= U'A' && value <= U'Z') || (value >= U'a' && value <= U'z');
}

inline bool is_ascii_digit(char32_t value) {
    return value >= U'0' && value <= U'9';
}

inline bool is_ascii_alnum(char32_t value) {
    return is_ascii_letter(value) || is_ascii_digit(value);
}

inline bool is_cjk(char32_t value) {
    return (value >= 0x3400 && value <= 0x4DBF) || (value >= 0x4E00 && value <= 0x9FFF) ||
           (value >= 0x3040 && value <= 0x30FF) || (value >= 0xAC00 && value <= 0xD7AF);
}

inline bool is_word_char(char32_t value) {
    return is_ascii_alnum(value) || is_cjk(value);
}

/// std::wregex is used for the template rules because MSVC has no
/// std::regex_traits<char32_t>. On Windows wchar_t is UTF-16, so the
/// conversion handles surrogate pairs; elsewhere wchar_t is already 32-bit.
inline std::wstring to_wide(std::u32string_view text) {
    std::wstring out;
    if constexpr (sizeof(wchar_t) == 2) {
        for (char32_t codepoint : text) {
            if (codepoint <= 0xFFFF) {
                out.push_back(static_cast<wchar_t>(codepoint));
            } else {
                const char32_t adjusted = codepoint - 0x10000;
                out.push_back(static_cast<wchar_t>(0xD800 + (adjusted >> 10)));
                out.push_back(static_cast<wchar_t>(0xDC00 + (adjusted & 0x3FF)));
            }
        }
    } else {
        out.assign(text.begin(), text.end());
    }
    return out;
}

inline std::u32string from_wide(std::wstring_view text) {
    std::u32string out;
    if constexpr (sizeof(wchar_t) == 2) {
        for (std::size_t index = 0; index < text.size(); ++index) {
            const auto unit = static_cast<std::uint16_t>(text[index]);
            if (unit >= 0xD800 && unit <= 0xDBFF && index + 1 < text.size()) {
                const auto low = static_cast<std::uint16_t>(text[index + 1]);
                if (low >= 0xDC00 && low <= 0xDFFF) {
                    out.push_back(static_cast<char32_t>(
                        0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00)));
                    ++index;
                    continue;
                }
            }
            out.push_back(static_cast<char32_t>(unit));
        }
    } else {
        out.assign(text.begin(), text.end());
    }
    return out;
}

inline std::u32string lower_ascii(std::u32string_view text) {
    std::u32string out(text);
    for (auto& value : out) {
        if (value >= U'A' && value <= U'Z') {
            value = static_cast<char32_t>(value + 32);
        }
    }
    return out;
}

inline std::string lower_ascii(std::string_view text) {
    std::string out(text);
    for (auto& value : out) {
        if (value >= 'A' && value <= 'Z') {
            value = static_cast<char>(value + 32);
        }
    }
    return out;
}

inline bool is_ascii_space(char32_t value) {
    return value == U' ' || value == U'\t' || value == U'\r' || value == U'\n' ||
           value == U'\f' || value == U'\v';
}

/// True when the string holds a non-whitespace character (Python's strip()).
inline bool has_non_space(std::u32string_view text) {
    for (char32_t value : text) {
        if (!is_ascii_space(value) && value != U'\u3000') {
            return true;
        }
    }
    return false;
}

}  // namespace watashi::text
