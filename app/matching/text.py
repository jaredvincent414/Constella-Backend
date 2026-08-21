"""Normalization helpers shared by the scorer and the clusterer.

Major and career-area names arrive as free text, so "Public Health" needs to
partially match "Health Policy" without matching "Public Policy" any harder
than it should. Everything here is deterministic — the same inputs always
produce the same score, which is what makes cached results safe to reuse.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Dropped before comparison because they carry no discriminating signal. Leaving
# "science" in would make "Computer Science" and "Political Science" look 33%
# similar on token overlap alone.
STOPWORDS = frozenset(
    {
        "a",
        "and",
        "applied",
        "for",
        "general",
        "in",
        "of",
        "science",
        "sciences",
        "studies",
        "the",
    }
)

_NON_WORD = re.compile(r"[^a-z0-9]+")


# Memoized because the scorer calls these on the *same* handful of strings over
# and over: every student is compared against every alumnus's major names,
# career area, and interests, so a corpus of 600 alumni re-tokenizes the same
# few hundred labels once per student. The functions are pure and deterministic
# — the property this module already relied on for cached results to be safe to
# reuse — so a cache changes nothing except how often the regex runs.
#
# maxsize is generous rather than unbounded: program and industry vocabularies
# are small, but course codes and free-text interests are not, and an unbounded
# cache on a long-lived worker is a leak with extra steps.


@lru_cache(maxsize=16_384)
def normalize(value: str) -> str:
    """Lowercase, collapse punctuation and whitespace to single spaces."""
    return _NON_WORD.sub(" ", value.lower()).strip()


@lru_cache(maxsize=16_384)
def tokenize(value: str) -> frozenset[str]:
    """Frozen because the result is shared between callers now.

    A plain set would let one caller's `|=` corrupt every later caller's view of
    the same label. Equality with a plain set is unaffected.
    """
    return frozenset(tok for tok in normalize(value).split() if tok and tok not in STOPWORDS)


def jaccard(left: frozenset[str] | set[str], right: frozenset[str] | set[str]) -> float:
    """Intersection over union. Two empty sets are undefined, not identical."""
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


@lru_cache(maxsize=65_536)
def text_similarity(left: str | None, right: str | None) -> float:
    """Fuzzy agreement between two free-text labels, in 0..1.

    Exact match after normalization short-circuits to 1.0; otherwise this is
    token-set Jaccard, so "Public Health" and "Health Policy" score 1/3 rather
    than 0 — a partial signal, which is what the 20% major-match component wants.
    """
    if not left or not right:
        return 0.0
    if normalize(left) == normalize(right):
        return 1.0
    return jaccard(tokenize(left), tokenize(right))


def best_text_similarity(candidate: str | None, options: list[str]) -> float:
    """Highest similarity between `candidate` and any of `options`."""
    if not candidate or not options:
        return 0.0
    return max(text_similarity(candidate, option) for option in options)


@lru_cache(maxsize=65_536)
def normalize_course_code(code: str) -> str:
    """'BIO 101', 'bio-101', and 'Bio101' are the same course."""
    return _NON_WORD.sub("", code.lower())
