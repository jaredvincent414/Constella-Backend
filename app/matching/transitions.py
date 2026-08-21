"""What-If transition cards, and the aggregates above them.

The Explore map answers "who is like me"; this answers "who made the move I'm
considering, and what did it look like". Same scoring engine, same corpus
filter — this module is the presenter that turns a scored candidate into the
card the Transition page renders, plus the three summary numbers in its header.

The aggregates are computed over **every** candidate the pivot query matched,
not just the handful shown. "12 alumni made this transition" is a fact about
the corpus; deriving it from the five cards on screen would make it a fact about
the page size instead.
"""

from __future__ import annotations

from collections import Counter

from app.matching.outcomes import build_outcome, outcome_industry
from app.matching.programs import detect_pivots
from app.matching.scoring import ScoredAlumnus, StudentProfile
from app.matching.timeline import build_semesters, course_display_name
from app.models import Alumnus, StudentYear, semester_label
from app.schemas import (
    TransitionCard,
    TransitionCourse,
    TransitionSemester,
)

# Courses named in the pre-pivot summary before it turns into a wall of text.
PRE_PIVOT_COURSE_SAMPLE = 3

# Year labels named in `peak_timing`. Beyond two the phrase stops being a
# finding and starts being a list.
PEAK_TIMING_MAX_YEARS = 2


def pre_pivot_summary(alumnus: Alumnus) -> str:
    """"Pre-pivot (3 semesters): Economics 101, Stats, Intro Economics".

    A description of what they were taking while they were still where the
    student is now — the same transcript slice the 50% course-overlap component
    scores against, so the card and the ranking are talking about the same
    thing.
    """
    courses = [c for c in alumnus.pre_pivot_courses() if not c.dropped]
    if not courses:
        return "No coursework recorded before the pivot"

    semesters = len({c.semester_index for c in courses})
    ordered = sorted(courses, key=lambda c: (c.semester_index, c.course_code))
    names = [course_display_name(c) for c in ordered[:PRE_PIVOT_COURSE_SAMPLE]]
    more = len(ordered) - len(names)
    listed = ", ".join(names) + (f", +{more} more" if more > 0 else "")
    return f"Pre-pivot ({semesters} semester{'' if semesters == 1 else 's'}): {listed}"


def _major_change(alumnus: Alumnus) -> str | None:
    """'added' | 'dropped' | 'switched' — how the major set actually changed.

    Set-diffed from the program timeline and matched to the stored pivot's term
    when there is one, so the card can distinguish "added a second major" from
    "switched majors" rather than calling both a pivot.
    """
    pivot = alumnus.first_pivot
    changes = [c for c in detect_pivots(alumnus) if c.role == "major"]
    if not changes:
        return None
    if pivot is not None:
        matched = next((c for c in changes if c.term == pivot.semester_index), None)
        if matched is not None:
            return matched.kind
    return changes[0].kind


def build_card(
    scored: ScoredAlumnus, profile: StudentProfile, is_top_match: bool
) -> TransitionCard:
    alumnus = scored.alumnus
    pivot = alumnus.first_pivot
    return TransitionCard(
        # The id is what lets a card open the same detail panel a constellation
        # node does. Without it the two views can't reference the same person.
        id=alumnus.id,
        is_top_match=is_top_match,
        class_year=alumnus.graduation_year,
        match_percent=round(scored.total * 100, 1),
        from_major=pivot.from_major if pivot else None,
        to_major=pivot.to_major if pivot else None,
        pivot_semester=semester_label(pivot.semester_index) if pivot else None,
        pivot_type=_major_change(alumnus),
        career_outcome=build_outcome(alumnus),
        pre_pivot_summary=pre_pivot_summary(alumnus),
        timeline=[
            TransitionSemester(
                semester=sem.label,
                pivot=sem.is_pivot,
                courses=[
                    TransitionCourse(name=course.name, tag=course.status)
                    for course in sem.courses
                ],
            )
            # Reuses the constellation's own timeline builder, so a course is
            # marked kept/new/dropped identically here and in the detail panel.
            for sem in build_semesters(alumnus, profile)
        ],
    )


def peak_timing(candidates: list[Alumnus]) -> str | None:
    """When this transition usually happens, in words.

    Returns None rather than a guess when nothing pivoted — an alumnus can match
    a destination without a recorded pivot, and "peaks in freshman year" would
    be an invented finding about an empty set.
    """
    years = Counter(
        alumnus.first_pivot.year_index for alumnus in candidates if alumnus.first_pivot
    )
    if not years:
        return None

    top = years.most_common()
    peak = top[0][1]
    # Every year tied for the mode, in chronological order — "sophomore and
    # junior year" is a real pattern, and picking one arbitrarily would hide it.
    tied = sorted(year for year, count in top if count == peak)[:PEAK_TIMING_MAX_YEARS]
    labels = [StudentYear.from_index(year).value for year in tied]
    if len(labels) == 1:
        return f"{labels[0]} year"
    return f"{' and '.join(labels)} year"


def top_outcome(candidates: list[Alumnus]) -> tuple[str | None, int]:
    """Where this transition most often leads, and how many got there.

    The industry, not the job title: titles are too granular to aggregate
    ("Analyst II" vs "Policy Analyst"), which is the same reason clustering
    keys on the coarser field.
    """
    industries = Counter(outcome_industry(alumnus) for alumnus in candidates)
    if not industries:
        return None, 0
    label, count = industries.most_common(1)[0]
    return label, count
