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

``scope`` is the rest of the key, and it exists because this memory used to be keyed on
the source text *alone*: switching the target language inside the TTL then served the
previous language's text back as if it were the new one -- a Chinese line handed to a
user who asked for Japanese, at full confidence, for ten seconds. The same collision
made "the same line, in a different situation" impossible to express, which is the whole
question a corpus of one word with two meanings rests on. So the key is
``(normalised source, *scope)``, and the engine decides what belongs in the scope: it
passes the parts of the context its *vocabulary* can actually distinguish, so a corpus
with no scene and no window conditions behaves exactly as before (see
``CorpusStore.context_key``).
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Sequence

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
        self._entries: "OrderedDict[tuple[str, ...], _Entry]" = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.stored = 0

    # -- keys -------------------------------------------------------------- #

    @staticmethod
    def _key(source: str, scope: Sequence[str] = ()) -> tuple[str, ...] | None:
        """The memory key, or None when this source is too short to remember.

        Always a tuple, even with an empty scope, so nothing downstream has to ask
        whether it is holding a string or a key.
        """
        base = normalize(source)
        if len(base) < MIN_KEY_LENGTH:
            return None
        return (base, *scope)

    # -- lookup ------------------------------------------------------------ #

    def get(
        self,
        source: str,
        now: float | None = None,
        scope: Sequence[str] = (),
    ) -> str | None:
        """The recent translation of ``source`` *in this scope*, or None.

        Reuse moves the entry to the end, so a phrase that keeps appearing on screen is
        not evicted by a burst of one-off lines -- it is the phrase most worth keeping.
        """
        if not self.enabled:
            return None
        key = self._key(source, scope)
        if key is None:
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

    def put(
        self,
        source: str,
        target: str,
        now: float | None = None,
        scope: Sequence[str] = (),
    ) -> None:
        """Remember a translation. Empty targets are not worth remembering."""
        if not self.enabled or not target.strip():
            return
        key = self._key(source, scope)
        if key is None:
            return
        now = time.perf_counter() if now is None else now
        self._entries[key] = _Entry(target=target, at=now)
        self._entries.move_to_end(key)
        self.stored += 1
        while len(self._entries) > self.limit:
            self._entries.popitem(last=False)

    # -- invalidation ------------------------------------------------------ #

    def drop(self, *sources: str) -> int:
        """Forget these sources, in **every** scope. Returns how many entries went.

        Exists for corrections. A memory that keeps serving the translation a human
        just rejected is worse than having no memory at all: the corrected corpus
        entry is ready and the cache would hide it for the rest of the TTL -- which
        is exactly the ten seconds in which the user is looking at the screen to
        check whether their correction worked.

        Every scope, not just the current one: the caller is saying "this source is
        wrong", which is true whichever scene or window it was read in, and a memory
        that keeps the rejected answer alive in another scope is the same bug one
        scene later.
        """
        wanted = {normalize(source) for source in sources}
        doomed = [key for key in self._entries if key[0] in wanted]
        for key in doomed:
            del self._entries[key]
        return len(doomed)

    def drop_containing(self, fragment: str) -> int:
        """Forget every remembered source that contains ``fragment``, in every scope.

        For term corrections: the term is one word inside a line, and the stale entry
        is the whole line's translation, so there is no single key to drop.
        """
        needle = normalize(fragment)
        if not needle:
            return 0
        doomed = [key for key in self._entries if needle in key[0]]
        for key in doomed:
            del self._entries[key]
        return len(doomed)

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
