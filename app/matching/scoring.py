"""Similarity scoring.

The weighted formula from the backend spec:

    | Factor                   | Weight |
    |--------------------------|--------|
    | Course overlap           |  50%   |
    | Pivot year alignment     |  20%   |
    | From/To major match      |  20%   |
    | Interest/activity overlap|  10%   |

Course overlap is deliberately **directional**: it measures the fraction of the
*student's* courses that the alumnus also took, not the reverse. An alumnus with
40 courses sharing 8 with you is exactly as relevant as one with 15 sharing 8 —
dividing by the alumnus's course count would penalize the former for no reason.

Vectorization
-------------
Course overlap is the expensive component: |students| x |alumni| x |courses|
set intersections. Building a boolean incidence matrix once and taking a single
matrix-vector product turns the whole corpus into one NumPy operation, which is
what keeps the background job fast enough to re-run on every profile change.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.matching.text import (
    best_text_similarity,
    jaccard,
    normalize_course_code,
    text_similarity,
    tokenize,
)
from app.models import Alumnus, Student

WEIGHT_COURSE_OVERLAP = 0.50
WEIGHT_PIVOT_YEAR = 0.20
WEIGHT_MAJOR_MATCH = 0.20
WEIGHT_INTEREST = 0.10

# Freshman (0) to senior (3). The widest possible gap between a student's
# current year and an alumnus's pivot year.
MAX_YEAR_DISTANCE = 3

# An alumnus who never pivoted has no pivot year to align against. Scoring them
# 0 would bury every straight-through path in a broad explore query, and scoring
# them 1.0 would rank them above alumni who genuinely pivoted at the right time.
# Neutral is the honest answer.
NO_PIVOT_ALIGNMENT = 0.5

# Used when the student hasn't supplied a side of the pivot query. Same
# reasoning: absence of information is not evidence of a mismatch.
UNSPECIFIED_MAJOR_MATCH = 0.5

# A candidate must be at least this close to the requested destination to appear
# in a What If simulation. Below this the alumnus pivoted somewhere else and
# their path answers a different question.
PIVOT_QUERY_THRESHOLD = 0.3


@dataclass
class ScoredAlumnus:
    alumnus: Alumnus
    total: float
    course_overlap: float
    pivot_year_alignment: float
    major_match: float
    interest_overlap: float
    shared_courses: list[str] = field(default_factory=list)


@dataclass
class StudentProfile:
    """The scorer's view of a student — plain data, no ORM session needed.

    Detaching this from the model keeps scoring pure and testable, and lets the
    What If Simulator score against a hypothetical profile that was never saved.
    """

    id: str
    year_index: int
    declared_major: str | None
    intended_direction: str | None
    interests: list[str]
    course_codes: list[str]
    course_names: dict[str, str]

    @classmethod
    def from_model(cls, student: Student) -> StudentProfile:
        codes: list[str] = []
        names: dict[str, str] = {}
        for course in student.courses:
            code = normalize_course_code(course.course_code)
            if code not in names:
                codes.append(code)
                names[code] = course.course_name
        return cls(
            id=student.id,
            year_index=student.year_index,
            declared_major=student.declared_major,
            intended_direction=student.intended_direction,
            interests=list(student.interests or []),
            course_codes=codes,
            course_names=names,
        )


# --------------------------------------------------------------------------
# Individual components
# --------------------------------------------------------------------------


def pivot_year_alignment(student_year_index: int, alumnus: Alumnus) -> float:
    """How closely the alumnus's pivot timing matches the student's year.

    A sophomore seeing alumni who pivoted as sophomores scores 1.0; the same
    sophomore seeing a senior-year pivot scores lower, because that path starts
    from a position they haven't reached yet.
    """
    pivot = alumnus.first_pivot
    if pivot is None:
        return NO_PIVOT_ALIGNMENT
    distance = abs(pivot.year_index - student_year_index)
    return max(0.0, 1.0 - distance / MAX_YEAR_DISTANCE)


def major_match(profile: StudentProfile, alumnus: Alumnus) -> float:
    """Agreement on both ends of the pivot: where they started, where they landed.

    Each side is worth half. A side the student left unspecified scores neutral
    rather than zero — an undeclared freshman shouldn't be penalized for being
    undeclared.
    """
    if profile.declared_major:
        from_score = text_similarity(profile.declared_major, alumnus.origin_major)
    else:
        from_score = UNSPECIFIED_MAJOR_MATCH

    if profile.intended_direction:
        # The destination may be phrased as a major ("Public Health") or as a
        # career area ("Health Policy"), so both are valid targets.
        targets = [*alumnus.final_majors, alumnus.career_area]
        to_score = best_text_similarity(profile.intended_direction, targets)
    else:
        to_score = UNSPECIFIED_MAJOR_MATCH

    return 0.5 * from_score + 0.5 * to_score


def interest_overlap(profile: StudentProfile, alumnus: Alumnus) -> float:
    """Jaccard over interest/activity tokens. A tiebreaker, not a primary signal."""
    student_tokens: set[str] = set()
    for interest in profile.interests:
        student_tokens |= tokenize(interest)
    alumnus_tokens: set[str] = set()
    for interest in alumnus.interests or []:
        alumnus_tokens |= tokenize(interest)
    return jaccard(student_tokens, alumnus_tokens)


# --------------------------------------------------------------------------
# Corpus scoring
# --------------------------------------------------------------------------


class CourseOverlapMatrix:
    """Boolean incidence matrix over pre-pivot transcripts.

    Rows are alumni, columns are the student's courses. Row sums after masking
    give every alumnus's shared-course count in one operation.
    """

    def __init__(self, alumni: list[Alumnus], student_course_codes: list[str]):
        self.alumni = alumni
        self.course_codes = student_course_codes
        self._column_of = {code: i for i, code in enumerate(student_course_codes)}
        self.matrix = np.zeros((len(alumni), len(student_course_codes)), dtype=bool)

        for row, alumnus in enumerate(alumni):
            for course in alumnus.pre_pivot_courses():
                # Dropped courses aren't part of what they actually completed.
                if course.dropped:
                    continue
                column = self._column_of.get(normalize_course_code(course.course_code))
                if column is not None:
                    self.matrix[row, column] = True

    def overlap_fractions(self) -> np.ndarray:
        """Directional overlap per alumnus: shared / total student courses."""
        if not self.course_codes:
            # A student with no courses on record gives the primary signal
            # nothing to work with. Return zeros and let the other 50% decide.
            return np.zeros(len(self.alumni), dtype=float)
        return self.matrix.sum(axis=1) / len(self.course_codes)

    def shared_codes(self, row: int) -> list[str]:
        return [self.course_codes[i] for i in np.flatnonzero(self.matrix[row])]


def score_corpus(profile: StudentProfile, alumni: list[Alumnus]) -> list[ScoredAlumnus]:
    """Score every alumnus against the student, ranked descending."""
    if not alumni:
        return []

    overlap = CourseOverlapMatrix(alumni, profile.course_codes)
    fractions = overlap.overlap_fractions()

    scored: list[ScoredAlumnus] = []
    for row, alumnus in enumerate(alumni):
        course_component = float(fractions[row])
        pivot_component = pivot_year_alignment(profile.year_index, alumnus)
        major_component = major_match(profile, alumnus)
        interest_component = interest_overlap(profile, alumnus)

        total = (
            WEIGHT_COURSE_OVERLAP * course_component
            + WEIGHT_PIVOT_YEAR * pivot_component
            + WEIGHT_MAJOR_MATCH * major_component
            + WEIGHT_INTEREST * interest_component
        )

        shared = [
            profile.course_names.get(code, code) for code in overlap.shared_codes(row)
        ]

        scored.append(
            ScoredAlumnus(
                alumnus=alumnus,
                total=round(min(1.0, max(0.0, total)), 4),
                course_overlap=round(course_component, 4),
                pivot_year_alignment=round(pivot_component, 4),
                major_match=round(major_component, 4),
                interest_overlap=round(interest_component, 4),
                shared_courses=shared,
            )
        )

    # Tie-break on id so equal scores don't reorder between runs — the frontend
    # builds spatial memory from a stable layout, and layout follows this order.
    scored.sort(key=lambda s: (-s.total, s.alumnus.id))
    return scored


def filter_by_pivot_query(
    alumni: list[Alumnus],
    from_major: str | None,
    to_major: str | None,
    threshold: float = PIVOT_QUERY_THRESHOLD,
) -> list[Alumnus]:
    """Narrow the corpus to alumni whose pivot answers the student's question.

    Only applied in What If mode. A broad explore query passes `to_major=None`
    and keeps the whole corpus.
    """
    if not to_major:
        return list(alumni)

    matches: list[Alumnus] = []
    for alumnus in alumni:
        targets = [*alumnus.final_majors, alumnus.career_area]
        if best_text_similarity(to_major, targets) < threshold:
            continue
        if from_major:
            origin_candidates = [alumnus.origin_major] if alumnus.origin_major else []
            pivot = alumnus.first_pivot
            if pivot:
                origin_candidates.append(pivot.from_major)
            if best_text_similarity(from_major, origin_candidates) < threshold:
                continue
        matches.append(alumnus)
    return matches
