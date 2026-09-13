"""Language identification and code mapping.

Extracted from ``local_nmt`` because these are *language* concerns, not *model*
concerns: the pipeline needs to decide "is this text already the language we are
translating into?" without caring which backend would have done the translating.

Why that decision matters: without it, Chinese on screen is "translated" into
Chinese. The user sees their own language rewritten, the local model spends
~300-600 ms per line rephrasing text that needed no translation, and the
subtitle keeps changing for no reason. Recognising source == target and passing
the text through untouched removes all of that.
"""

from __future__ import annotations

import re

#: project language tag -> NLLB-200 language code
NLLB_CODES: dict[str, str] = {
    "zh": "zho_Hans",
    "zh-cn": "zho_Hans",
    "zh-hans": "zho_Hans",
    "zh-tw": "zho_Hant",
    "zh-hant": "zho_Hant",
    "en": "eng_Latn",
    "ja": "jpn_Jpan",
    "ko": "kor_Hang",
    "fr": "fra_Latn",
    "de": "deu_Latn",
    "es": "spa_Latn",
    "pt": "por_Latn",
    "it": "ita_Latn",
    "ru": "rus_Cyrl",
    "ar": "arb_Arab",
    "th": "tha_Thai",
    "vi": "vie_Latn",
    "id": "ind_Latn",
}

#: Languages that do not separate words with spaces.
UNSPACED = ("zho", "jpn", "kor", "tha")

#: Scripts that identify a language on their own, checked in order. Kana means
#: Japanese (Chinese never uses it), Hangul means Korean, and so on. These must
#: be consulted *before* Han: a Japanese sentence is mostly kanji, so letting
#: character counts decide would classify it as Chinese.
_DECISIVE_SCRIPTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[\u3040-\u30ff]"), "jpn_Jpan"),
    (re.compile(r"[\uac00-\ud7af]"), "kor_Hang"),
    (re.compile(r"[\u0e00-\u0e7f]"), "tha_Thai"),
    (re.compile(r"[\u0600-\u06ff]"), "arb_Arab"),
    (re.compile(r"[\u0400-\u04ff]"), "rus_Cyrl"),
)

#: Han characters are shared by Chinese and Japanese, so this is only reached
#: when no decisive script was present.
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

#: A script has to cover this fraction of the text to decide the language, so a
#: stray glyph inside an English sentence does not flip the verdict.
SCRIPT_SHARE = 0.10
HAN_SHARE = 0.15

_LATIN_LETTER = re.compile(r"[A-Za-z]")

#: Any Unicode letter: a word character that is not a digit or underscore. Covers
#: Han, kana, hangul and Latin alike, which is what "is there anything here worth
#: translating?" needs -- as opposed to "which language is it?".
_ANY_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def to_nllb_code(tag: str) -> str:
    """Map a project language tag to an NLLB code, passing NLLB codes through."""
    key = tag.strip().lower().replace("_", "-")
    if key in NLLB_CODES:
        return NLLB_CODES[key]
    if re.fullmatch(r"[a-z]{3}_[A-Z][a-z]{3}", tag):
        return tag
    raise ValueError(f"no NLLB code known for language {tag!r}")


def decisive_language(text: str) -> str | None:
    """The language, but only when the script identifies it by itself.

    Returns ``None`` when the verdict would rest on the Latin fallback ("there were
    letters") or on there being no letters at all.

    That distinction is the whole point. A script verdict for Han, kana, hangul,
    Thai, Cyrillic or Arabic is trustworthy; a verdict of "English" from the mere
    presence of Latin letters is not, because it cannot tell French from English.
    The language gate needs to know which kind it is holding: it should trust a
    real script verdict over the user's declaration, and trust the declaration over
    a Latin guess. Collapsing both into one answer is how Chinese text ended up
    being translated every frame when a source language was declared.
    """
    if not text.strip():
        return None
    length = max(1, len(text))

    for pattern, language in _DECISIVE_SCRIPTS:
        hits = len(pattern.findall(text))
        if hits and hits / length >= SCRIPT_SHARE:
            return language

    han = len(_HAN.findall(text))
    if han and han / length >= HAN_SHARE:
        return "zho_Hans"

    return None


def detect_language(text: str, default: str = "eng_Latn") -> str:
    """Best-effort source language, from the script actually present.

    Deliberately a script heuristic rather than a statistical detector: it needs
    no dependency, and the only question that matters here is "is this already
    the target language?", which a script check answers reliably for the
    language pairs this project targets.

    Known limitation: Japanese written entirely in kanji (no kana) reads as
    Chinese, because the scripts are genuinely identical, and any Latin-script
    language other than English reads as English. A real detector is the fix for
    both; :func:`decisive_language` lets callers find out whether the answer here
    was a real script match or just that fallback.
    """
    decisive = decisive_language(text)
    if decisive is not None:
        return decisive
    if _LATIN_LETTER.search(text):
        return "eng_Latn"
    return default


def has_translatable_content(text: str) -> bool:
    """True when the text holds at least one letter, in any script.

    Digits, punctuation and symbols are not translatable, and sending them to a
    model buys nothing but noise. This is not a micro-optimisation: a screen with a
    clock, a countdown, a progress counter or a row of symbols produces a fresh
    "sentence" on every tick, each one missing the translation cache, so the model
    runs continuously on text that has no language to translate.
    """
    return bool(_ANY_LETTER.search(text))



def same_language(a: str, b: str) -> bool:
    """True when two tags or codes denote the same language, ignoring script.

    ``zh-CN`` and ``zho_Hans`` are the same language; so are ``zh`` and
    ``zho_Hant``, which is a simplification on purpose -- treating them as equal
    means Traditional text is passed through rather than needlessly converted,
    and a conversion the user did not ask for is the louder failure.
    """
    try:
        left = to_nllb_code(a).split("_")[0]
        right = to_nllb_code(b).split("_")[0]
    except ValueError:
        return False
    return left == right


def matches_target(text: str, target_lang: str) -> bool:
    """True when *text* is already written in *target_lang*.

    Shares :func:`detect_language` rather than reimplementing the script rules,
    so the two can never disagree -- an earlier version did, and classified a
    Japanese line as Chinese.
    """
    if not text.strip():
        return False
    try:
        target = to_nllb_code(target_lang)
    except ValueError:
        return False
    detected = detect_language(text, default="")
    if not detected:
        return False
    return same_language(detected, target)
