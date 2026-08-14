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

from app.config import settings
from app.matching.outcomes import outcome_industry
from app.matching.programs import (
    all_minors,
    origin_majors,
    student_majors,
    student_minors,
)
from app.matching.programs import (
    final_majors as final_major_set,
)
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
    # A student's majors/minors are always sets — size 1 in the placeholder data,
    # but the scorer must never assume that. `declared_major` above is a
    # convenience scalar (the primary) and a fallback when no program rows exist.
    current_majors: set[str] = field(default_factory=set)
    current_minors: set[str] = field(default_factory=set)

    @property
    def major_set(self) -> set[str]:
        """Current majors as a set — never a bare string, even for one major."""
        if self.current_majors:
            return set(self.current_majors)
        return {self.declared_major} if self.declared_major else set()

    @property
    def minor_set(self) -> set[str]:
        return set(self.current_minors)

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
            current_majors=student_majors(student),
            current_minors=student_minors(student),
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


def soft_jaccard(left: set[str], right: set[str]) -> float:
    """Weighted Jaccard over two program sets, using fuzzy name similarity as the
    element kernel rather than exact equality.

    For singletons this reduces exactly to `text_similarity`, so a single-major
    student's score is unchanged from the pre-set-based formula. For larger sets
    it rewards partial overlap: CS+Art vs CS-only lands strictly between 0 and 1
    (the shared major matches, the unshared one doesn't), never collapsing to
    equality's 0-or-1.
    """
    if not left or not right:
        return 0.0
    left_best = sum(max(text_similarity(a, b) for b in right) for a in left)
    right_best = sum(max(text_similarity(b, a) for a in left) for b in right)
    return (left_best + right_best) / (len(left) + len(right))


def major_match(profile: StudentProfile, alumnus: Alumnus) -> float:
    """Agreement on both ends of the pivot: where they started, where they landed.

    Set-based throughout — a student or alumnus may hold several majors, and
    minors contribute at a reduced, configurable weight. Each end of the pivot is
    worth half; a side the student left unspecified scores neutral rather than
    zero, so an undeclared freshman isn't penalized for being undeclared.
    """
    student_majors = profile.major_set
    if student_majors:
        from_score = soft_jaccard(student_majors, origin_majors(alumnus))
    else:
        from_score = UNSPECIFIED_MAJOR_MATCH

    if profile.intended_direction:
        # The destination may be phrased as a major ("Public Health"), the
        # academic area, or the industry the alumnus landed in ("Healthcare") —
        # all are valid targets for "where do I want to end up".
        targets = [*final_major_set(alumnus), alumnus.career_area, outcome_industry(alumnus)]
        to_score = best_text_similarity(profile.intended_direction, targets)
    else:
        to_score = UNSPECIFIED_MAJOR_MATCH

    major_component = 0.5 * from_score + 0.5 * to_score

    # Minors nudge the score without swinging it. They only participate when both
    # sides actually have minors, so single-major records are byte-for-byte
    # unchanged and an alumnus isn't penalized for holding extra minors.
    student_minors = profile.minor_set
    alumnus_minors = all_minors(alumnus)
    if student_minors and alumnus_minors:
        minor_component = soft_jaccard(student_minors, alumnus_minors)
        w = settings.minor_match_weight
        return (major_component + w * minor_component) / (1 + w)
    return major_component


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
        targets = [*alumnus.final_majors, alumnus.career_area, outcome_industry(alumnus)]
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
