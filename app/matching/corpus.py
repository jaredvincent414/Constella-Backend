"""A prepared view of an alumni corpus.

The scorer used to derive everything it needed straight off the ORM objects, per
student. That looked free and wasn't: an alumnus's normalized pre-pivot course
codes, their origin and final majors, their minors, their interest tokens, and
their pivot year are all *student-independent*, yet every one of them was
recomputed for every student — regexes, interval reconstruction over program
rows, and roughly a million instrumented SQLAlchemy attribute reads per ten
students against a 600-alumnus corpus.

This module computes that once. `build_corpus` is the boundary: past it, scoring
touches plain Python data and never an ORM attribute.

    corpus = build_corpus(alumni, school_id=student.school_id)
    for student in that_school:
        score_corpus(profile, corpus)          # no re-derivation

`score_corpus` and `build_constellation` still accept a bare list of alumni and
build a corpus themselves, so a caller that scores one student — every request
path, and every existing test — needs no changes. The win is in the nightly job,
which scores many students against one corpus.

Nothing here is a scoring decision. Every field is produced by calling the same
accessor the scorer called inline, so the values are identical by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.matching.outcomes import outcome_industry
from app.matching.programs import all_minors, origin_majors
from app.matching.programs import final_majors as final_major_set
from app.matching.text import normalize_course_code, tokenize
from app.models import Alumnus


@dataclass(frozen=True)
class AlumnusView:
    """One alumnus, with everything the scorer derives from them precomputed.

    The `set` fields are deliberately not frozensets. `soft_jaccard` sums a
    `max()` per element, so iteration order decides float summation order, and
    `frozenset(s)` is not guaranteed to iterate in the same order as `s`. Holding
    the exact set object the original accessor returned keeps the arithmetic
    bit-for-bit identical to computing it inline. Nothing mutates them.
    """

    alumnus: Alumnus

    # Course overlap: normalized codes from the pre-pivot transcript, dropped
    # courses already excluded.
    pre_pivot_codes: frozenset[str]

    # Major match.
    origin_majors: set[str]
    final_majors: set[str]
    minors: set[str]
    # `[*final_majors, career_area, industry]` — the destinations a student's
    # intended direction is matched against. Order is irrelevant downstream
    # (it feeds a `max`), but it is fixed here so it can't vary between calls.
    destinations: tuple[str, ...] = field(default=())

    # Pivot-year alignment. None when they never pivoted.
    pivot_year_index: int | None = None

    # `filter_by_pivot_query` reads the ORM-side accessors rather than the
    # program-table ones, so both are carried rather than assumed equal.
    pivot_destinations: tuple[str, ...] = field(default=())
    pivot_origins: tuple[str, ...] = field(default=())

    # Interest overlap.
    interest_tokens: frozenset[str] = frozenset()

    @property
    def id(self) -> str:
        return self.alumnus.id


@dataclass(frozen=True)
class Corpus:
    """A school's alumni, prepared for scoring.

    `school_id` travels with the data on purpose. The nightly job builds one
    corpus per school and scores that school's students against it, and the
    tenant boundary is only as good as the guarantee that those two agree — so
    the scorer can assert it rather than trust the caller to have grouped
    correctly.
    """

    views: tuple[AlumnusView, ...]
    school_id: str | None = None

    def __len__(self) -> int:
        return len(self.views)

    def __iter__(self):
        return iter(self.views)

    @property
    def alumni(self) -> list[Alumnus]:
        return [view.alumnus for view in self.views]


def build_view(alumnus: Alumnus) -> AlumnusView:
    pivot = alumnus.first_pivot

    interest_tokens: set[str] = set()
    for interest in alumnus.interests or []:
        interest_tokens |= tokenize(interest)

    final = final_major_set(alumnus)
    origins = origin_majors(alumnus)

    pivot_origins = [alumnus.origin_major] if alumnus.origin_major else []
    if pivot is not None:
        pivot_origins.append(pivot.from_major)

    return AlumnusView(
        alumnus=alumnus,
        pre_pivot_codes=frozenset(
            normalize_course_code(course.course_code)
            for course in alumnus.pre_pivot_courses()
            # Dropped courses aren't part of what they actually completed.
            if not course.dropped
        ),
        origin_majors=origins,
        final_majors=final,
        minors=all_minors(alumnus),
        destinations=(*final, alumnus.career_area, outcome_industry(alumnus)),
        pivot_year_index=pivot.year_index if pivot is not None else None,
        pivot_destinations=(
            *alumnus.final_majors,
            alumnus.career_area,
            outcome_industry(alumnus),
        ),
        pivot_origins=tuple(pivot_origins),
        interest_tokens=frozenset(interest_tokens),
    )


def build_corpus(alumni: list[Alumnus], school_id: str | None = None) -> Corpus:
    """Prepare a corpus for scoring. Costs one pass; saves one pass per student."""
    return Corpus(views=tuple(build_view(a) for a in alumni), school_id=school_id)


def as_corpus(alumni: list[Alumnus] | Corpus, school_id: str | None = None) -> Corpus:
    """Accept either an already-prepared corpus or a raw list.

    Lets one-student callers stay as they are while the job prepares once.
    """
    if isinstance(alumni, Corpus):
        return alumni
    return build_corpus(alumni, school_id=school_id)
