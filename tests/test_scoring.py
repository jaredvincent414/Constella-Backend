"""Tests for the weighted similarity formula."""

from __future__ import annotations

import pytest

from app.matching.scoring import (
    NO_PIVOT_ALIGNMENT,
    WEIGHT_COURSE_OVERLAP,
    WEIGHT_INTEREST,
    WEIGHT_MAJOR_MATCH,
    WEIGHT_PIVOT_YEAR,
    interest_overlap,
    major_match,
    pivot_year_alignment,
    score_corpus,
)
from tests.factories import make_alumnus, make_profile


def test_weights_sum_to_one():
    total = (
        WEIGHT_COURSE_OVERLAP + WEIGHT_PIVOT_YEAR + WEIGHT_MAJOR_MATCH + WEIGHT_INTEREST
    )
    assert total == pytest.approx(1.0)


class TestCourseOverlap:
    def test_is_directional(self):
        """Overlap divides by the *student's* course count, not the alumnus's.

        The spec's example: an alumnus with 40 courses sharing 8 with you is as
        relevant as one with 15 sharing 8. Both must score identically.
        """
        profile = make_profile(
            courses=[(f"C{i}", f"Course {i}") for i in range(8)],
            declared_major=None,
        )
        shared = [(f"C{i}", f"Course {i}", 0) for i in range(8)]

        prolific = make_alumnus(
            "prolific",
            courses=shared + [(f"X{i}", f"Extra {i}", 1) for i in range(32)],
            pivot_semester=None,
            final_major=None,
        )
        focused = make_alumnus(
            "focused",
            courses=shared + [(f"Y{i}", f"Other {i}", 1) for i in range(7)],
            pivot_semester=None,
            final_major=None,
        )

        scored = {s.alumnus.id: s for s in score_corpus(profile, [prolific, focused])}
        assert scored["prolific"].course_overlap == 1.0
        assert scored["focused"].course_overlap == 1.0
        assert scored["prolific"].total == scored["focused"].total

    def test_counts_only_pre_pivot_courses(self):
        """Courses taken after the pivot don't count toward overlap.

        The signal is "we were in the same place before they changed direction".
        """
        profile = make_profile(courses=[("A", "Alpha"), ("B", "Beta")])
        alumnus = make_alumnus(
            courses=[("A", "Alpha", 0), ("B", "Beta", 4)],
            pivot_semester=2,
        )
        scored = score_corpus(profile, [alumnus])[0]
        assert scored.course_overlap == 0.5
        assert scored.shared_courses == ["Alpha"]

    def test_dropped_courses_excluded(self):
        profile = make_profile(courses=[("A", "Alpha"), ("B", "Beta")])
        alumnus = make_alumnus(
            courses=[("A", "Alpha", 0)],
            dropped=[("B", "Beta", 1)],
            pivot_semester=None,
            final_major=None,
        )
        assert score_corpus(profile, [alumnus])[0].course_overlap == 0.5

    def test_course_code_formatting_ignored(self):
        profile = make_profile(courses=[("BIO 101", "Bio 101")])
        alumnus = make_alumnus(
            courses=[("bio-101", "Bio 101", 0)], pivot_semester=None, final_major=None
        )
        assert score_corpus(profile, [alumnus])[0].course_overlap == 1.0

    def test_student_with_no_courses_scores_zero_overlap(self):
        profile = make_profile(courses=[])
        alumnus = make_alumnus(courses=[("A", "Alpha", 0)])
        assert score_corpus(profile, [alumnus])[0].course_overlap == 0.0


class TestPivotYearAlignment:
    def test_same_year_is_perfect(self):
        sophomore_pivot = make_alumnus(pivot_semester=3)  # 3 // 2 == year 1
        assert pivot_year_alignment(1, sophomore_pivot) == 1.0

    def test_decays_with_distance(self):
        senior_pivot = make_alumnus(pivot_semester=6)  # year 3
        freshman, senior = 0, 3
        assert pivot_year_alignment(senior, senior_pivot) == 1.0
        assert pivot_year_alignment(freshman, senior_pivot) == pytest.approx(0.0)

    def test_no_pivot_is_neutral(self):
        straight_through = make_alumnus(pivot_semester=None, final_major=None)
        assert pivot_year_alignment(1, straight_through) == NO_PIVOT_ALIGNMENT


class TestMajorMatch:
    def test_exact_both_sides(self):
        profile = make_profile(declared_major="Biochemistry", intended_direction="Public Health")
        alumnus = make_alumnus(origin_major="Biochemistry", final_major="Public Health")
        assert major_match(profile, alumnus) == 1.0

    def test_matches_career_area_when_direction_is_a_career(self):
        """The destination may be phrased as a career, not a major."""
        profile = make_profile(declared_major="Biochemistry", intended_direction="Health Policy")
        alumnus = make_alumnus(
            origin_major="Biochemistry", final_major="Public Health", career_area="Health Policy"
        )
        assert major_match(profile, alumnus) == 1.0

    def test_undeclared_student_is_not_penalized(self):
        """An undeclared freshman scores neutral, not zero."""
        profile = make_profile(declared_major=None, intended_direction=None)
        assert major_match(profile, make_alumnus()) == 0.5

    def test_partial_token_overlap(self):
        profile = make_profile(declared_major="Public Health", intended_direction=None)
        alumnus = make_alumnus(origin_major="Health Policy")
        score = major_match(profile, alumnus)
        # "health" shared out of {public, health, policy} = 1/3, halved with the
        # neutral 0.5 on the unspecified side.
        assert score == pytest.approx(0.5 * (1 / 3) + 0.5 * 0.5)


class TestInterestOverlap:
    def test_jaccard(self):
        profile = make_profile(interests=["Global Health Club"])
        alumnus = make_alumnus(interests=["Global Health Club"])
        assert interest_overlap(profile, alumnus) == 1.0

    def test_no_interests_is_zero_not_one(self):
        """Two empty sets are undefined, not identical."""
        profile = make_profile(interests=[])
        assert interest_overlap(profile, make_alumnus(interests=[])) == 0.0


class TestRanking:
    def test_sorted_descending_and_deterministic(self):
        profile = make_profile(courses=[("A", "Alpha"), ("B", "Beta")])
        alumni = [
            make_alumnus("high", courses=[("A", "Alpha", 0), ("B", "Beta", 0)], pivot_semester=3),
            make_alumnus("low", courses=[], pivot_semester=6),
            make_alumnus("mid", courses=[("A", "Alpha", 0)], pivot_semester=3),
        ]
        first = [s.alumnus.id for s in score_corpus(profile, alumni)]
        assert first == ["high", "mid", "low"]
        # Byte-identical across runs: the frontend's spatial memory depends on it.
        assert first == [s.alumnus.id for s in score_corpus(profile, list(reversed(alumni)))]

    def test_ties_break_on_id(self):
        profile = make_profile(courses=[("A", "Alpha")], declared_major=None)
        alumni = [
            make_alumnus("zzz", courses=[("A", "Alpha", 0)], pivot_semester=3),
            make_alumnus("aaa", courses=[("A", "Alpha", 0)], pivot_semester=3),
        ]
        assert [s.alumnus.id for s in score_corpus(profile, alumni)] == ["aaa", "zzz"]

    def test_total_is_the_weighted_sum(self):
        profile = make_profile(
            courses=[("A", "Alpha")],
            declared_major="Biochemistry",
            intended_direction="Public Health",
            interests=["Research"],
        )
        alumnus = make_alumnus(
            courses=[("A", "Alpha", 0)], pivot_semester=3, interests=["Research"]
        )
        s = score_corpus(profile, [alumnus])[0]
        expected = (
            WEIGHT_COURSE_OVERLAP * s.course_overlap
            + WEIGHT_PIVOT_YEAR * s.pivot_year_alignment
            + WEIGHT_MAJOR_MATCH * s.major_match
            + WEIGHT_INTEREST * s.interest_overlap
        )
        assert s.total == pytest.approx(expected, abs=1e-4)

    def test_empty_corpus(self):
        assert score_corpus(make_profile(), []) == []
