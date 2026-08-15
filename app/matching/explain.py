"""One-line rationale for why an alumnus matched.

Turns the score breakdown the engine already computes into something a student
can read: "shared Genetics and Organic Chem, and changed direction sophomore
year — the year you're in now."

Two rules make this trustworthy rather than decorative:

**Generated from the components, never narrated.** Every clause is derived from
the same numbers that produced the ranking, so the rationale cannot disagree
with the order the nodes are in. A model asked to explain a ranking after the
fact will produce fluent, plausible reasons for an order it never saw — and the
first time those two diverge, the feature has taught the student to distrust the
map.

**Describes the overlap, never the destination.** A clause like "ended up in
Health Policy" would state a career outcome as fact, and on the placeholder
dataset those outcomes are `provenance='synthetic'` — fabricated. Explanations
are exactly where invented data reads as authoritative, so this module talks
only about what the student and the alumnus have *in common*: courses, timing,
where they started, shared interests. The destination is already in the payload
next to its provenance flag, where the UI can label it.

A factor appears only when it actually contributed. No shared courses means no
course clause — not a hedged one.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.matching.programs import origin_majors
from app.matching.text import tokenize
from app.models import YEAR_LABELS, Alumnus

# A pivot has to land within a year of the student's own to be worth mentioning;
# that is alignment >= 1 - 1/3. Below it, "changed direction" is true of almost
# everyone and says nothing about this pair.
MIN_PIVOT_ALIGNMENT = 0.66

# Course names listed inline before collapsing to "+N more". Three is what fits
# on one line in the detail panel.
MAX_NAMED_COURSES = 3

# Clauses in the summary. More than three stops being a sentence.
MAX_FACTORS = 3


@dataclass(frozen=True)
class MatchReason:
    """`summary` is the sentence; `factors` are the same clauses unjoined, for a
    UI that would rather render chips than prose."""

    summary: str
    factors: list[str]


def _course_clause(shared_courses: list[str]) -> str | None:
    """Name the shared courses, falling back to a count when they're untitled.

    MIDFIELD carries courses with blank names, and naming those renders "shared
    , , and " — which is worse than not naming them at all. The count is still
    true and still specific, so the clause survives rather than disappearing.
    """
    if not shared_courses:
        return None
    total = len(shared_courses)
    named = sorted({name.strip() for name in shared_courses if name and name.strip()})
    if not named:
        return f"shared {total} course{'s' if total > 1 else ''}"

    items = named[:MAX_NAMED_COURSES]
    # Counted against the total, so unnamed courses are still accounted for
    # rather than silently dropped from the tally.
    remaining = total - len(items)
    if remaining > 0:
        items = [*items, f"{remaining} more course{'s' if remaining > 1 else ''}"]
    return f"shared {_join(items)}"


def _major_clause(profile, alumnus: Alumnus) -> str | None:
    """Only when the origin majors genuinely intersect.

    The score uses fuzzy name similarity, which is right for ranking but too
    loose to assert in prose — "started in Biochemistry too" should mean they
    did, not that the names looked alike.
    """
    shared = profile.major_set & origin_majors(alumnus)
    if not shared:
        return None
    return f"started in {_join(sorted(shared))}, like you"


def _pivot_clause(profile, alumnus: Alumnus, alignment: float) -> str | None:
    pivot = alumnus.first_pivot
    if pivot is None or alignment < MIN_PIVOT_ALIGNMENT:
        return None
    year = YEAR_LABELS[min(pivot.year_index, len(YEAR_LABELS) - 1)].lower()
    if pivot.year_index == profile.year_index:
        return f"changed direction {year} year — where you are now"
    return f"changed direction {year} year"


def _interest_clause(profile, alumnus: Alumnus) -> str | None:
    student_tokens: set[str] = set()
    for interest in profile.interests:
        student_tokens |= tokenize(interest)
    if not student_tokens:
        return None
    shared = [i for i in (alumnus.interests or []) if tokenize(i) & student_tokens]
    if not shared:
        return None
    return f"also in {_join(sorted(shared)[:2])}"


def _join(items: list[str]) -> str:
    """Oxford-comma join: [a] -> 'a', [a,b] -> 'a and b', [a,b,c] -> 'a, b, and c'."""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def explain_match(scored, alumnus: Alumnus, profile) -> MatchReason | None:
    """Why this alumnus surfaced, or None when nothing specific did.

    None is a real answer: an alumnus can rank into the top 200 on the neutral
    defaults alone (no pivot to misalign, no major to contradict) without sharing
    anything with the student. Inventing a reason for those is precisely the
    failure this module is shaped to avoid.
    """
    if scored is None:
        return None

    factors = [
        clause
        for clause in (
            _course_clause(scored.shared_courses),
            _major_clause(profile, alumnus),
            _pivot_clause(profile, alumnus, scored.pivot_year_alignment),
            _interest_clause(profile, alumnus),
        )
        if clause
    ][:MAX_FACTORS]

    if not factors:
        return None

    summary = _join(factors)
    return MatchReason(summary=summary[0].upper() + summary[1:], factors=factors)
