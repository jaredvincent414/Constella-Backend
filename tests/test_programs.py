"""Acceptance tests for multiple-majors / multiple-minors support.

These are the checks that fail if the single-major assumption creeps back in or
if the real-data swap would break. Numbered to the task's list.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.matching.programs import (
    detect_pivots,
    majors_at,
    minors_at,
    student_majors,
    student_minors,
)
from app.matching.scoring import major_match
from app.models import ProgramRole, Student, StudentProgram
from tests.factories import make_alumnus, make_profile


# --- 6.1 double major + two minors round-trips through schema + accessors ----
def test_double_major_two_minors_round_trip():
    # An alumnus with two majors and two minors, built through the ORM models.
    alumnus = make_alumnus(
        "am",
        origin_major="Computer Science",
        final_major=None,  # CS is final (primary), not replaced
        pivot_semester=None,
        second_majors=[("Mathematics", 2)],
        minors=[("Studio Art", 3), ("Music", 4)],
    )
    # Every program round-trips; the accessors return sets, never scalars.
    assert majors_at(alumnus, 7) == {"Computer Science", "Mathematics"}
    assert minors_at(alumnus, 7) == {"Studio Art", "Music"}
    assert isinstance(majors_at(alumnus, 7), set)

    # Same on the query side via StudentProgram rows.
    student = Student(id="sp", declared_major=None)
    student.programs = [
        StudentProgram(name="Computer Science", term=4, role=ProgramRole.primary),
        StudentProgram(name="Mathematics", term=4, role=ProgramRole.second_major),
        StudentProgram(name="Studio Art", term=4, role=ProgramRole.minor),
        StudentProgram(name="Music", term=4, role=ProgramRole.minor),
    ]
    assert student_majors(student) == {"Computer Science", "Mathematics"}
    assert student_minors(student) == {"Studio Art", "Music"}


# --- 6.2 CS+Art major-match vs CS-only is strictly between 0 and 1 -----------
def test_double_major_scores_partial_against_single_major():
    profile = make_profile(declared_major=None, intended_direction=None)
    profile.current_majors = {"Computer Science", "Studio Art"}

    cs_only = make_alumnus("cs", origin_major="Computer Science", final_major=None,
                           pivot_semester=None)
    score = major_match(profile, cs_only)
    # The 'from' half is a partial set overlap; the unspecified 'to' half is 0.5.
    from_only = 0.5 * 0.5  # to-side neutral halved
    assert 0.0 < score < 1.0
    assert score > from_only  # the shared CS major does contribute


def test_single_major_still_reduces_to_exact():
    """A one-major set behaves exactly like the old scalar comparison."""
    profile = make_profile(declared_major="Computer Science", intended_direction=None)
    exact = make_alumnus("cs", origin_major="Computer Science", final_major=None,
                         pivot_semester=None)
    # from = text_similarity(CS, CS) = 1.0, to unspecified = 0.5.
    assert major_match(profile, exact) == pytest.approx(0.5 * 1.0 + 0.5 * 0.5)


# --- 6.3 pivot type: added vs switched --------------------------------------
def test_pivot_added_when_second_major_appears():
    alumnus = make_alumnus(
        "add",
        origin_major="Computer Science",
        final_major=None,          # CS stays (is_final)
        pivot_semester=None,
        second_majors=[("Mathematics", 3)],
    )
    major_pivots = [p for p in detect_pivots(alumnus) if p.role == "major"]
    assert len(major_pivots) == 1
    assert major_pivots[0].kind == "added"
    assert major_pivots[0].added == frozenset({"Mathematics"})


def test_pivot_switched_when_major_replaced():
    alumnus = make_alumnus(
        "switch",
        origin_major="Computer Science",
        final_major="Economics",   # CS not final -> replaced at pivot
        pivot_semester=3,
    )
    major_pivots = [p for p in detect_pivots(alumnus) if p.role == "major"]
    assert len(major_pivots) == 1
    assert major_pivots[0].kind == "switched"
    assert major_pivots[0].added == frozenset({"Economics"})
    assert major_pivots[0].removed == frozenset({"Computer Science"})


def test_minor_change_is_its_own_pivot():
    alumnus = make_alumnus(
        "min",
        origin_major="Computer Science",
        final_major=None,
        pivot_semester=None,
        minors=[("Music", 4)],
    )
    minor_pivots = [p for p in detect_pivots(alumnus) if p.role == "minor"]
    assert [p.kind for p in minor_pivots] == ["added"]


# --- 6.4 regression: single-major total score unchanged ---------------------
def test_single_major_total_is_unchanged_regression():
    """The exact worked example from the scoring tests, pinned numerically.

    If the set-based refactor shifted a single-major score, this breaks.
    """
    from app.matching.scoring import (
        WEIGHT_COURSE_OVERLAP,
        WEIGHT_INTEREST,
        WEIGHT_MAJOR_MATCH,
        WEIGHT_PIVOT_YEAR,
        score_corpus,
    )

    profile = make_profile(
        courses=[("A", "Alpha")],
        declared_major="Biochemistry",
        intended_direction="Public Health",
        interests=["Research"],
    )
    alumnus = make_alumnus(courses=[("A", "Alpha", 0)], pivot_semester=3, interests=["Research"])
    s = score_corpus(profile, [alumnus])[0]

    # course=1.0, pivot(year1 vs pivot sem3->year1)=1.0, major exact=1.0, interest=1.0
    assert s.course_overlap == 1.0
    assert s.pivot_year_alignment == 1.0
    assert s.major_match == 1.0
    assert s.interest_overlap == 1.0
    expected = (
        WEIGHT_COURSE_OVERLAP + WEIGHT_PIVOT_YEAR + WEIGHT_MAJOR_MATCH + WEIGHT_INTEREST
    )
    assert s.total == pytest.approx(round(min(1.0, expected), 4))


# --- 6.5 no module outside the accessor layer reads the program field -------
def test_only_the_accessor_layer_reads_program_rows():
    """Serving code (matching, api) must go through app/matching/programs.py —
    never reconstruct a major set by walking `.majors`/`.programs` or reading
    `.is_final`."""
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    read_layer = [
        p
        for p in [*(root / "matching").glob("*.py"), *(root / "api").rglob("*.py")]
        if p.name != "programs.py"
    ]
    forbidden = re.compile(r"\.majors\b|\.programs\b|\.is_final\b")
    offenders = {}
    for path in read_layer:
        hits = [
            line.strip()
            for line in path.read_text().splitlines()
            # Import lines reference the module path `app.matching.programs`; they
            # aren't reads of a program field.
            if forbidden.search(line) and not line.lstrip().startswith(("from ", "import "))
        ]
        if hits:
            offenders[str(path.relative_to(root))] = hits
    assert not offenders, f"program field read outside the accessor layer: {offenders}"
