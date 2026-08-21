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

Everything the scorer needs from an alumnus that does *not* depend on the
student — normalized pre-pivot course codes, origin and final majors, minors,
interest tokens, pivot year — is precomputed in `app.matching.corpus` and passed
in as an `AlumnusView`. Scoring a corpus therefore touches no ORM attributes and
runs no regexes. The public component functions still accept a bare `Alumnus`
and build a view on the spot, so calling them on one record reads the same as it
always did.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.config import settings
from app.matching.corpus import AlumnusView, Corpus, as_corpus, build_view
from app.matching.programs import student_majors, student_minors
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


def _view(alumnus: Alumnus | AlumnusView) -> AlumnusView:
    """Accept a prepared view or a raw record.

    The corpus path always passes views; a caller scoring a single alumnus pays
    for one view instead of restructuring around a corpus it doesn't have.
    """
    return alumnus if isinstance(alumnus, AlumnusView) else build_view(alumnus)


def pivot_year_alignment(student_year_index: int, alumnus: Alumnus | AlumnusView) -> float:
    """How closely the alumnus's pivot timing matches the student's year.

    A sophomore seeing alumni who pivoted as sophomores scores 1.0; the same
    sophomore seeing a senior-year pivot scores lower, because that path starts
    from a position they haven't reached yet.
    """
    pivot_year = _view(alumnus).pivot_year_index
    if pivot_year is None:
        return NO_PIVOT_ALIGNMENT
    distance = abs(pivot_year - student_year_index)
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


def _major_match(
    student_majors: set[str],
    student_minors: set[str],
    intended_direction: str | None,
    view: AlumnusView,
) -> float:
    """The scoring body, with the student's side already resolved.

    Split out because `profile.major_set` and `profile.minor_set` each build a
    fresh set on every access — once per alumnus, for a value that is the same
    for the whole corpus.
    """
    if student_majors:
        from_score = soft_jaccard(student_majors, view.origin_majors)
    else:
        from_score = UNSPECIFIED_MAJOR_MATCH

    if intended_direction:
        # The destination may be phrased as a major ("Public Health"), the
        # academic area, or the industry the alumnus landed in ("Healthcare") —
        # all are valid targets for "where do I want to end up".
        to_score = best_text_similarity(intended_direction, view.destinations)
    else:
        to_score = UNSPECIFIED_MAJOR_MATCH

    major_component = 0.5 * from_score + 0.5 * to_score

    # Minors nudge the score without swinging it. They only participate when both
    # sides actually have minors, so single-major records are byte-for-byte
    # unchanged and an alumnus isn't penalized for holding extra minors.
    if student_minors and view.minors:
        minor_component = soft_jaccard(student_minors, view.minors)
        w = settings.minor_match_weight
        return (major_component + w * minor_component) / (1 + w)
    return major_component


def major_match(profile: StudentProfile, alumnus: Alumnus | AlumnusView) -> float:
    """Agreement on both ends of the pivot: where they started, where they landed.

    Set-based throughout — a student or alumnus may hold several majors, and
    minors contribute at a reduced, configurable weight. Each end of the pivot is
    worth half; a side the student left unspecified scores neutral rather than
    zero, so an undeclared freshman isn't penalized for being undeclared.
    """
    return _major_match(
        profile.major_set, profile.minor_set, profile.intended_direction, _view(alumnus)
    )


def student_interest_tokens(profile: StudentProfile) -> frozenset[str]:
    tokens: set[str] = set()
    for interest in profile.interests:
        tokens |= tokenize(interest)
    return frozenset(tokens)


def interest_overlap(profile: StudentProfile, alumnus: Alumnus | AlumnusView) -> float:
    """Jaccard over interest/activity tokens. A tiebreaker, not a primary signal."""
    return jaccard(student_interest_tokens(profile), _view(alumnus).interest_tokens)


# --------------------------------------------------------------------------
# Corpus scoring
# --------------------------------------------------------------------------


class CourseOverlapMatrix:
    """Boolean incidence matrix over pre-pivot transcripts.

    Rows are alumni, columns are the student's courses. Row sums after masking
    give every alumnus's shared-course count in one operation.
    """

    def __init__(self, views: tuple[AlumnusView, ...], student_course_codes: list[str]):
        self.views = views
        self.course_codes = student_course_codes
        self._column_of = {code: i for i, code in enumerate(student_course_codes)}
        self.matrix = np.zeros((len(views), len(student_course_codes)), dtype=bool)

        for row, view in enumerate(views):
            # The view's codes are already normalized and already exclude dropped
            # courses, so filling a row is a C-level set intersection rather than
            # a walk over ORM rows with a regex per course code.
            for code in view.pre_pivot_codes.intersection(self._column_of):
                self.matrix[row, self._column_of[code]] = True

    def overlap_fractions(self) -> np.ndarray:
        """Directional overlap per alumnus: shared / total student courses."""
        if not self.course_codes:
            # A student with no courses on record gives the primary signal
            # nothing to work with. Return zeros and let the other 50% decide.
            return np.zeros(len(self.views), dtype=float)
        return self.matrix.sum(axis=1) / len(self.course_codes)

    def shared_codes(self, row: int) -> list[str]:
        return [self.course_codes[i] for i in np.flatnonzero(self.matrix[row])]


def score_corpus(
    profile: StudentProfile, alumni: list[Alumnus] | Corpus, top_n: int | None = None
) -> list[ScoredAlumnus]:
    """Score every alumnus against the student, ranked descending.

    Takes a prepared `Corpus` or a raw list. The nightly job builds the corpus
    once per school and passes the same one for every student at that school;
    a list is prepared on the spot, which is what every single-student caller
    does.

    `top_n` cuts the corpus *before* the per-result work rather than after. The
    constellation keeps 200 of a corpus that is heading for tens of thousands,
    and the discarded 99.6% were each paying for four `round` calls, a
    `flatnonzero` over their overlap row, a shared-course name lookup, and a
    dataclass — then being dropped by a full sort. Ranking needs only the total;
    everything else is built for survivors.

    Omitted, the whole corpus comes back scored and sorted, which is what the
    saved-paths route and the eval harness want.
    """
    corpus = as_corpus(alumni)
    views = corpus.views
    if not views:
        return []

    overlap = CourseOverlapMatrix(views, profile.course_codes)
    fractions = overlap.overlap_fractions()

    # The student's side of every component, resolved once instead of per
    # alumnus. `major_set` and `minor_set` each build a new set per access.
    student_major_set = profile.major_set
    student_minor_set = profile.minor_set
    intended_direction = profile.intended_direction
    interest_tokens = student_interest_tokens(profile)

    components: list[tuple[float, float, float, float]] = []
    totals = np.empty(len(views), dtype=float)
    for row, view in enumerate(views):
        course_component = float(fractions[row])
        pivot_component = pivot_year_alignment(profile.year_index, view)
        major_component = _major_match(
            student_major_set, student_minor_set, intended_direction, view
        )
        interest_component = jaccard(interest_tokens, view.interest_tokens)
        components.append(
            (course_component, pivot_component, major_component, interest_component)
        )
        total = (
            WEIGHT_COURSE_OVERLAP * course_component
            + WEIGHT_PIVOT_YEAR * pivot_component
            + WEIGHT_MAJOR_MATCH * major_component
            + WEIGHT_INTEREST * interest_component
        )
        # Rounded before ranking, not after: rounding creates ties, and the id
        # tie-break has to see the same ties the response will show.
        totals[row] = round(min(1.0, max(0.0, total)), 4)

    order = _rank(views, totals, top_n)

    scored: list[ScoredAlumnus] = []
    for row in order:
        course_component, pivot_component, major_component, interest_component = components[row]
        scored.append(
            ScoredAlumnus(
                alumnus=views[row].alumnus,
                # Back to a Python float: numpy scalars leak into the response
                # models and the JSON encoder, and nothing downstream wants them.
                total=float(totals[row]),
                course_overlap=round(course_component, 4),
                pivot_year_alignment=round(pivot_component, 4),
                major_match=round(major_component, 4),
                interest_overlap=round(interest_component, 4),
                shared_courses=[
                    profile.course_names.get(code, code) for code in overlap.shared_codes(row)
                ],
            )
        )
    return scored


def _rank(
    views: tuple[AlumnusView, ...], totals: np.ndarray, top_n: int | None
) -> list[int]:
    """Row indices in response order: total descending, then alumnus id.

    The id tie-break keeps equal scores from reordering between runs — the
    frontend builds spatial memory from a stable layout, and layout follows this
    order.

    For a top-N cut, `argpartition` finds the cut in linear time instead of
    sorting the corpus. The partition alone isn't enough: it splits on total and
    breaks ties arbitrarily, so a tie straddling the boundary could admit the
    wrong member. Widening the candidate set to everything at or above the
    boundary value, then sorting only that, gives exactly the same answer as a
    full sort.
    """
    count = len(views)
    if top_n is None or top_n >= count:
        return sorted(range(count), key=lambda row: (-totals[row], views[row].id))

    partition = np.argpartition(-totals, top_n - 1)[:top_n]
    boundary = totals[partition].min()
    candidates = np.flatnonzero(totals >= boundary)
    ordered = sorted(candidates.tolist(), key=lambda row: (-totals[row], views[row].id))
    return ordered[:top_n]


def _answers_pivot_query(
    view: AlumnusView, from_major: str | None, to_major: str, threshold: float
) -> bool:
    if best_text_similarity(to_major, view.pivot_destinations) < threshold:
        return False
    if from_major and best_text_similarity(from_major, view.pivot_origins) < threshold:
        return False
    return True


def filter_by_pivot_query(
    alumni: list[Alumnus] | Corpus,
    from_major: str | None,
    to_major: str | None,
    threshold: float = PIVOT_QUERY_THRESHOLD,
) -> list[Alumnus] | Corpus:
    """Narrow the corpus to alumni whose pivot answers the student's question.

    Only applied in What If mode. A broad explore query passes `to_major=None`
    and keeps the whole corpus.

    Returns whatever it was given — a `Corpus` in, a filtered `Corpus` out — so
    a prepared corpus survives the filter instead of being torn back down to
    records and rebuilt by the scorer.
    """
    if isinstance(alumni, Corpus):
        if not to_major:
            return alumni
        kept = tuple(
            view
            for view in alumni.views
            if _answers_pivot_query(view, from_major, to_major, threshold)
        )
        return Corpus(views=kept, school_id=alumni.school_id)

    if not to_major:
        return list(alumni)
    corpus = as_corpus(alumni)
    return [
        view.alumnus
        for view in corpus.views
        if _answers_pivot_query(view, from_major, to_major, threshold)
    ]
