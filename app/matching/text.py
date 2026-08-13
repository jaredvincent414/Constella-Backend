"""Normalization helpers shared by the scorer and the clusterer.

Major and career-area names arrive as free text, so "Public Health" needs to
partially match "Health Policy" without matching "Public Policy" any harder
than it should. Everything here is deterministic — the same inputs always
produce the same score, which is what makes cached results safe to reuse.
"""

from __future__ import annotations

import re

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


def normalize(value: str) -> str:
    """Lowercase, collapse punctuation and whitespace to single spaces."""
    return _NON_WORD.sub(" ", value.lower()).strip()


def tokenize(value: str) -> set[str]:
    return {tok for tok in normalize(value).split() if tok and tok not in STOPWORDS}


def jaccard(left: set[str], right: set[str]) -> float:
    """Intersection over union. Two empty sets are undefined, not identical."""
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


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


def normalize_course_code(code: str) -> str:
    """'BIO 101', 'bio-101', and 'Bio101' are the same course."""
    return _NON_WORD.sub("", code.lower())
