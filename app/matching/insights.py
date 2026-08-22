"""Corpus intelligence — patterns visible across the corpus, not from any single match.

The scorer answers "who is like me"; this module answers "what do people like me
tend to do". It takes the same `Corpus` the scorer uses and a `StudentProfile`,
so it costs no extra DB access when the corpus is already built.

Every function here is pure: corpus in, data out, no side effects.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from app.matching.corpus import AlumnusView, Corpus
from app.matching.scoring import StudentProfile


@dataclass(frozen=True)
class TransitionPattern:
    """A from → to major transition observed in the corpus."""

    from_major: str
    to_major: str
    count: int
    typical_semester: str | None  # e.g. "sophomore" — when most made the switch


@dataclass(frozen=True)
class OutcomeBreakdown:
    """One career area and how many relevant alumni ended up there."""

    career_area: str
    count: int
    percent: float


@dataclass(frozen=True)
class CourseSignal:
    """A course the student has taken, and where alumni who also took it ended up."""

    course_code: str
    top_outcome: str
    alumni_count: int


@dataclass(frozen=True)
class CorpusInsights:
    """Personalized intelligence derived from a school's alumni corpus."""

    # Where people who started like you went
    common_transitions: list[TransitionPattern]
    # What career areas alumni with your starting major ended up in
    outcome_distribution: list[OutcomeBreakdown]
    # Which of your courses correlate most with specific outcomes
    course_signals: list[CourseSignal]
    # When people with your major typically changed direction
    pivot_timing: str | None
    # How many alumni in the corpus started where you are
    cohort_size: int


def _semester_label(index: int) -> str:
    """0-7 semester index to a year label."""
    labels = [
        "freshman", "freshman", "sophomore", "sophomore",
        "junior", "junior", "senior", "senior",
    ]
    if 0 <= index < len(labels):
        return labels[index]
    return "senior"


def _find_cohort(corpus: Corpus, profile: StudentProfile) -> list[AlumnusView]:
    """Alumni who share a starting major with the student."""
    if not profile.major_set:
        return list(corpus.views)
    return [
        view for view in corpus.views
        if view.origin_majors & profile.major_set
    ]


def build_insights(
    profile: StudentProfile,
    corpus: Corpus,
    *,
    max_transitions: int = 5,
    max_outcomes: int = 6,
    max_signals: int = 5,
) -> CorpusInsights:
    """Analyze the corpus through the lens of one student's profile."""
    cohort = _find_cohort(corpus, profile)
    cohort_size = len(cohort)

    common_transitions = _transitions(cohort, max_transitions)
    outcome_distribution = _outcomes(cohort, max_outcomes)
    course_signals = _course_signals(cohort, profile, max_signals)
    pivot_timing = _pivot_timing(cohort)

    return CorpusInsights(
        common_transitions=common_transitions,
        outcome_distribution=outcome_distribution,
        course_signals=course_signals,
        pivot_timing=pivot_timing,
        cohort_size=cohort_size,
    )


def _transitions(cohort: list[AlumnusView], limit: int) -> list[TransitionPattern]:
    """Most common major changes among the cohort."""
    transition_counts: Counter[tuple[str, str]] = Counter()
    transition_timings: dict[tuple[str, str], list[int]] = {}

    for view in cohort:
        if view.pivot_year_index is None:
            continue
        for origin in view.origin_majors:
            for final in view.final_majors:
                if origin != final:
                    key = (origin, final)
                    transition_counts[key] += 1
                    transition_timings.setdefault(key, []).append(view.pivot_year_index)

    result = []
    for (from_major, to_major), count in transition_counts.most_common(limit):
        timings = transition_timings[(from_major, to_major)]
        # Median timing gives the typical semester
        timings.sort()
        median_idx = timings[len(timings) // 2]
        typical = _semester_label(median_idx)

        result.append(TransitionPattern(
            from_major=from_major,
            to_major=to_major,
            count=count,
            typical_semester=typical,
        ))
    return result


def _outcomes(cohort: list[AlumnusView], limit: int) -> list[OutcomeBreakdown]:
    """Career area distribution for the cohort."""
    counts: Counter[str] = Counter()
    for view in cohort:
        # outcome_labels is (career_area, industry) — use the first non-empty
        for label in view.outcome_labels:
            if label:
                counts[label] += 1
                break

    total = sum(counts.values()) or 1
    return [
        OutcomeBreakdown(
            career_area=area,
            count=count,
            percent=round(count / total * 100, 1),
        )
        for area, count in counts.most_common(limit)
    ]


def _course_signals(
    cohort: list[AlumnusView], profile: StudentProfile, limit: int
) -> list[CourseSignal]:
    """Which of the student's courses correlate with specific outcomes."""
    student_codes = set(profile.course_codes)
    if not student_codes:
        return []

    # For each student course, count which outcome alumni who also took it landed in
    course_outcomes: dict[str, Counter[str]] = {}
    for view in cohort:
        outcome = ""
        for label in view.outcome_labels:
            if label:
                outcome = label
                break
        if not outcome:
            continue

        shared = student_codes & view.pre_pivot_codes
        for code in shared:
            course_outcomes.setdefault(code, Counter())[outcome] += 1

    # Rank courses by how many alumni shared them (strongest signal first)
    ranked = sorted(
        course_outcomes.items(),
        key=lambda item: sum(item[1].values()),
        reverse=True,
    )

    result = []
    for code, outcomes in ranked[:limit]:
        top_outcome, top_count = outcomes.most_common(1)[0]
        result.append(CourseSignal(
            course_code=code,
            top_outcome=top_outcome,
            alumni_count=sum(outcomes.values()),
        ))
    return result


def _pivot_timing(cohort: list[AlumnusView]) -> str | None:
    """When people in this cohort typically changed direction."""
    timings = [
        view.pivot_year_index
        for view in cohort
        if view.pivot_year_index is not None
    ]
    if not timings:
        return None

    counts: Counter[str] = Counter()
    for idx in timings:
        counts[_semester_label(idx)] += 1

    # Top two timing periods
    top = counts.most_common(2)
    if len(top) == 1:
        return f"{top[0][0]} year"
    return f"{top[0][0]} and {top[1][0]} year"
