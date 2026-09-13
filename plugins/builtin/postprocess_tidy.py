"""Example plugin: tidy a finished translation.

Serves two purposes: it is genuinely useful (it strips the artefacts that OCR and
the model leave behind), and it is the smallest complete example of the
``postprocess`` contract -- one module, one manifest, one function.

Enable or disable it by editing ``PLUGIN["enabled"]`` below, or by removing the
file. Nothing else in the application needs to change.

See ``prototype/watashi/plugins.py`` for the contract and the trust caveat: this
runs in-process, so it is for your own machine.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

PLUGIN = {
    "name": "postprocess-tidy",
    "version": "0.2",
    "api": 1,
    "description": "collapse whitespace and strip OCR artefacts from translations",
    "points": ["postprocess"],
}

_CJK = r"\u3400-\u4dbf\u4e00-\u9fff"


def _collapse_split_glyphs(match: re.Match[str]) -> str:
    """Rejoin CJK that OCR read one glyph at a time.

    A blanket "remove spaces between CJK" rule is tempting and wrong: it merges
    words that the pipeline deliberately separated. "打得好，打得漂亮 新手" became
    "打得好，打得漂亮新手", and "反龙 超灵力 虚空剑" became one run -- the same
    regression that was already found and removed in the engine's span joining.

    So this only fires on the actual artefact: three or more single CJK glyphs
    each separated by exactly one space, which is OCR splitting, not phrase
    separation.
    """
    return match.group(0).replace(" ", "")


#: (pattern, replacement). The replacement may be a callable.
_RULES: tuple[tuple[str, Any], ...] = (
    # stray spaces before punctuation, common when a term was substituted in
    (r"\s+([，。！？；：、,.!?;:])", r"\1"),
    # OCR reading CJK one glyph at a time: "剑 意 是 个" -> "剑意是个"
    (rf"(?<![{_CJK}])(?:[{_CJK}] ){{2,}}[{_CJK}](?![{_CJK}])", _collapse_split_glyphs),
    # the vertical bars OCR reads out of table borders and UI separators
    (r"\s*[|丨]\s*", " "),
    # repeated punctuation from a partially recognised line
    (r"(。){2,}", "。"),
    (r"(\.){3,}", "..."),
)


class TidyPostprocessor:
    """Normalises whitespace and punctuation. Never drops information."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.compiled = [(re.compile(pattern), replacement) for pattern, replacement in _RULES]
        self.calls = 0

    def postprocess(self, text: str, context: dict[str, Any] | None = None) -> str | None:
        if not self.enabled or not text:
            return None
        self.calls += 1

        result = unicodedata.normalize("NFC", text)
        # the zero-width characters OCR emits around CJK and inside soft hyphens
        result = result.replace("\u200b", "").replace("\ufeff", "").replace("\u00ad", "")
        for pattern, replacement in self.compiled:
            result = pattern.sub(replacement, result)

        # collapse runs of spaces, but keep real line breaks: a multi-line
        # subtitle frame carries its structure in them
        lines = [" ".join(line.split()) for line in result.split("\n")]
        result = "\n".join(lines).strip()

        return result if result != text else None


def register() -> dict[str, object]:
    return {"postprocess": TidyPostprocessor()}
