"""Ranking and distribution metrics. Pure functions — no ORM, no session.

Kept free of the corpus so they can be unit-tested against hand-built rankings,
the same way the matching engine is kept session-free.
"""

from __future__ import annotations

import math
import statistics as st
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Description:
    """Summary of one component's values across a corpus."""

    count: int
    minimum: float
    median: float
    maximum: float
    mean: float
    stdev: float
    distinct: int

    @property
    def is_constant(self) -> bool:
        """A component with one value across the corpus ranks nothing.

        It still consumes its weight in the weighted sum, so it shifts every
        score by the same amount and contributes exactly zero ordering
        information. This is the check that would have caught `major_match`
        sitting at 0.25 for every alumnus.
        """
        return self.distinct <= 1


def describe(values: Sequence[float]) -> Description:
    if not values:
        return Description(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)
    return Description(
        count=len(values),
        minimum=min(values),
        median=st.median(values),
        maximum=max(values),
        mean=st.fmean(values),
        stdev=st.pstdev(values) if len(values) > 1 else 0.0,
        distinct=len(set(values)),
    )


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of all relevant items that appear in the top k.

    Undefined with no relevant items; returns 0.0 so a caller averaging over
    many queries doesn't have to special-case it (those queries are filtered out
    before they get here).
    """
    if not relevant:
        return 0.0
    hits = len(set(ranked[:k]) & relevant)
    return hits / len(relevant)


def precision_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of the top k that is relevant.

    This is the one to read against `base_rate`: precision@k equal to the base
    rate means the ranking is doing nothing a random draw wouldn't.
    """
    if k <= 0 or not ranked:
        return 0.0
    top = ranked[:k]
    return len(set(top) & relevant) / len(top)


def reciprocal_rank(ranked: Sequence[str], relevant: set[str]) -> float:
    """1/rank of the first relevant item, 0.0 if none appears."""
    for index, item in enumerate(ranked, start=1):
        if item in relevant:
            return 1.0 / index
    return 0.0


def base_rate(corpus_size: int, relevant: set[str]) -> float:
    """Share of the corpus that is relevant — the score random ranking earns.

    Every precision@k below is meaningless without this. A corpus that is 40%
    Engineering makes precision@10 = 0.4 exactly as good as shuffling.
    """
    if corpus_size <= 0:
        return 0.0
    return len(relevant) / corpus_size


def _ranks(values: Sequence[float]) -> list[float]:
    """Ascending ranks, averaging ties (the standard Spearman correction)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 + 1
        for position in order[i : j + 1]:
            ranks[position] = shared
        i = j + 1
    return ranks


def spearman(left: Sequence[float], right: Sequence[float]) -> float:
    """Rank correlation between two scorings of the same items.

    Used for ablations: drop a component, re-score, and compare orderings. 1.0
    means removing it changed no ranking at all — the component is inert on this
    corpus regardless of what its raw distribution looks like.

    Returns 1.0 when either side is constant: no ordering to disagree with.
    """
    if len(left) != len(right):
        raise ValueError("spearman needs two equal-length sequences")
    if len(left) < 2:
        return 1.0
    a, b = _ranks(left), _ranks(right)
    mean_a, mean_b = st.fmean(a), st.fmean(b)
    da = [x - mean_a for x in a]
    db = [y - mean_b for y in b]
    denominator = math.sqrt(sum(x * x for x in da) * sum(y * y for y in db))
    if denominator == 0:
        return 1.0
    return sum(x * y for x, y in zip(da, db, strict=True)) / denominator
