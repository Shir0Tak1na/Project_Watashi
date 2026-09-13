"""Remembering what has just been translated, so it is not translated again.

The problem this solves is not "the user sees a duplicate"; that is only the symptom.
The cost is the model: OCR is not deterministic, so the same on-screen sentence comes
back as ``他突破到了虚空境界``, then ``他突破到了虚空境界。``, then with a trailing space.
Each variant is a different cache key, so each one misses the translation cache and
re-runs the model -- on text that was already translated a moment ago. A clock or a
counter makes it worse, because every tick is a fresh sentence.

So the key is a *normalised* form of the source text, and normalisation is the part
that decides whether this works at all: too strict and nothing ever matches, too loose
and genuinely different lines get collapsed into one.

A bounded, time-limited memory rather than a permanent cache, because the same words
recur legitimately. A song lyric repeating, or a character saying the same thing twice
in a scene, must be translated again rather than silently reused from minutes ago.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from dataclasses import dataclass

#: Punctuation that OCR adds, drops or substitutes between otherwise identical frames.
#: Stripped from both ends before comparing, never from the middle: internal
#: punctuation can change meaning.
_EDGE_PUNCTUATION = "。，、！？；：…—－-.,!?;:\"'“”‘’()（）[]【】<>《》·~～` \t\u3000"

_WHITESPACE = re.compile(r"\s+")

#: A line has to be at least this long before it is remembered. Short strings collide
#: constantly ("是", "好", "OK"), and reusing a translation for a collision would be
#: wrong in a way that is very hard to notice.
MIN_KEY_LENGTH = 4


def normalize(text: str) -> str:
    """The comparison key for a recognised line.

    Collapses whitespace, trims edge punctuation, and case-folds Latin text, because
    none of those differences mean the sentence changed. Deliberately does *not* strip
    internal punctuation: ``好，我来`` and ``好我来`` are different sentences.
    """
    collapsed = _WHITESPACE.sub("", text.strip())
    stripped = collapsed.strip(_EDGE_PUNCTUATION)
    return stripped.casefold()


@dataclass
class _Entry:
    target: str
    at: float
    hits: int = 0


class RecentTranslations:
    """A short-term memory of source -> translation, with a time limit.

    ``hits`` counts reuse, which is the number that proves the mechanism is doing
    something: without it a memory that never matches looks exactly like one that
    works.
    """

    def __init__(self, ttl_s: float = 10.0, limit: int = 256, enabled: bool = True) -> None:
        self.ttl_s = max(0.0, float(ttl_s))
        self.limit = max(1, int(limit))
        self.enabled = bool(enabled)
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.stored = 0

    # -- lookup ------------------------------------------------------------ #

    def get(self, source: str, now: float | None = None) -> str | None:
        """The recent translation of ``source``, or None.

        Reuse moves the entry to the end, so a phrase that keeps appearing on screen is
        not evicted by a burst of one-off lines -- it is the phrase most worth keeping.
        """
        if not self.enabled:
            return None
        key = normalize(source)
        if len(key) < MIN_KEY_LENGTH:
            self.misses += 1
            return None
        entry = self._entries.get(key)
        now = time.perf_counter() if now is None else now
        if entry is None:
            self.misses += 1
            return None
        if now - entry.at > self.ttl_s:
            # Expired. Dropped rather than reused: the text may have meant something
            # else in a different scene, and a stale translation is harder to notice
            # than a repeated one.
            del self._entries[key]
            self.misses += 1
            return None
        entry.hits += 1
        entry.at = now
        self._entries.move_to_end(key)
        self.hits += 1
        return entry.target

    def put(self, source: str, target: str, now: float | None = None) -> None:
        """Remember a translation. Empty targets are not worth remembering."""
        if not self.enabled or not target.strip():
            return
        key = normalize(source)
        if len(key) < MIN_KEY_LENGTH:
            return
        now = time.perf_counter() if now is None else now
        self._entries[key] = _Entry(target=target, at=now)
        self._entries.move_to_end(key)
        self.stored += 1
        while len(self._entries) > self.limit:
            self._entries.popitem(last=False)

    # -- introspection ----------------------------------------------------- #

    def clear(self) -> None:
        self._entries.clear()
        self.hits = 0
        self.misses = 0
        self.stored = 0

    def __len__(self) -> int:
        return len(self._entries)

    def stats(self) -> dict[str, int | float]:
        lookups = self.hits + self.misses
        return {
            "dedup_enabled": int(self.enabled),
            "dedup_ttl_s": round(self.ttl_s, 2),
            "dedup_size": len(self._entries),
            "dedup_hits": self.hits,
            "dedup_misses": self.misses,
            "dedup_hit_rate": round(self.hits / lookups, 3) if lookups else 0.0,
            "dedup_stored": self.stored,
        }
