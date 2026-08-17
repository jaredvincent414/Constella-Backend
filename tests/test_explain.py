"""Tests for the match rationale.

The rationale is the one place fabricated data would read as authoritative, so
these pin both halves of the contract: every clause is backed by a real score
component, and nothing asserts the alumnus's career outcome.
"""

from __future__ import annotations

from app.matching.explain import MAX_FACTORS, explain_match
from app.matching.scoring import score_corpus
from tests.factories import make_alumnus, make_profile


def _scored(profile, alumnus):
    return score_corpus(profile, [alumnus])[0]


def _reason(profile, alumnus):
    return explain_match(_scored(profile, alumnus), alumnus, profile)


class TestCourseClause:
    def test_names_the_shared_courses(self):
        profile = make_profile(courses=[("BIO 101", "Bio 101"), ("CHEM 210", "Organic Chem")])
        alumnus = make_alumnus(
            courses=[("BIO 101", "Bio 101", 0), ("CHEM 210", "Organic Chem", 1)],
            pivot_semester=None,
            final_major=None,
        )
        reason = _reason(profile, alumnus)
        assert "Bio 101" in reason.summary
        assert "Organic Chem" in reason.summary

    def test_silent_when_no_courses_are_shared(self):
        profile = make_profile(courses=[("BIO 101", "Bio 101")])
        alumnus = make_alumnus(
            courses=[("FIN 310", "Corporate Finance", 0)],
            pivot_semester=None,
            final_major=None,
            origin_major="Economics",
        )
        reason = _reason(profile, alumnus)
        assert reason is None or "shared" not in reason.summary

    def test_collapses_a_long_list(self):
        shared = [(f"C{i}", f"Course {i}") for i in range(6)]
        profile = make_profile(courses=shared)
        alumnus = make_alumnus(
            courses=[(c, n, 0) for c, n in shared], pivot_semester=None, final_major=None
        )
        summary = _reason(profile, alumnus).summary
        assert "3 more courses" in summary


class TestMajorClause:
    def test_reports_a_genuine_shared_origin(self):
        profile = make_profile(declared_major="Biochemistry")
        profile.current_majors = {"Biochemistry"}
        alumnus = make_alumnus(origin_major="Biochemistry", final_major="Public Health")
        assert "started in Biochemistry, like you" in _reason(profile, alumnus).factors

    def test_requires_a_real_intersection_not_a_fuzzy_one(self):
        """The score uses fuzzy name similarity — good for ranking, too loose to
        assert in prose. 'Biochemistry' must not become 'started in Chemistry'."""
        profile = make_profile(declared_major="Biochemistry")
        profile.current_majors = {"Biochemistry"}
        alumnus = make_alumnus(origin_major="Chemistry", final_major="Public Health")
        reason = _reason(profile, alumnus)
        assert reason is None or "started in" not in reason.summary


class TestPivotClause:
    def test_calls_out_a_pivot_in_the_students_year(self):
        profile = make_profile(year_index=1, declared_major=None)  # sophomore
        alumnus = make_alumnus(pivot_semester=2)  # year index 1
        assert "where you are now" in _reason(profile, alumnus).summary

    def test_mentions_a_near_pivot_without_claiming_alignment(self):
        profile = make_profile(year_index=1, declared_major=None)
        alumnus = make_alumnus(pivot_semester=4)  # year index 2 — one year off
        reason = _reason(profile, alumnus)
        assert "changed direction junior year" in reason.factors
        assert "where you are now" not in reason.summary

    def test_silent_for_a_distant_pivot(self):
        profile = make_profile(year_index=0, declared_major=None)  # freshman
        alumnus = make_alumnus(pivot_semester=7)  # senior — three years away
        reason = _reason(profile, alumnus)
        assert reason is None or "changed direction" not in reason.summary

    def test_silent_when_there_was_no_pivot(self):
        profile = make_profile(declared_major=None)
        alumnus = make_alumnus(pivot_semester=None, final_major=None)
        reason = _reason(profile, alumnus)
        assert reason is None or "changed direction" not in reason.summary


class TestInterestClause:
    def test_reports_a_shared_interest(self):
        profile = make_profile(interests=["Global Health Club"], declared_major=None)
        alumnus = make_alumnus(
            interests=["Global Health Club"], pivot_semester=None, final_major=None
        )
        assert "also in Global Health Club" in _reason(profile, alumnus).factors


class TestContract:
    def test_never_asserts_the_career_outcome(self):
        """Outcomes are provenance='synthetic' on the placeholder dataset. A
        rationale stating one as fact is exactly how fabricated data acquires
        authority, so no clause may mention the destination."""
        profile = make_profile(
            courses=[("BIO 101", "Bio 101")],
            declared_major="Biochemistry",
            interests=["Research"],
        )
        profile.current_majors = {"Biochemistry"}
        alumnus = make_alumnus(
            courses=[("BIO 101", "Bio 101", 0)],
            pivot_semester=2,
            career_area="Health Policy",
            industry="Healthcare",
            occupation="Policy Analyst",
            interests=["Research"],
        )
        summary = _reason(profile, alumnus).summary
        for forbidden in ("Health Policy", "Healthcare", "Policy Analyst", "Public Health"):
            assert forbidden not in summary

    def test_returns_none_when_nothing_specific_matched(self):
        """An alumnus can rank on neutral defaults alone. Inventing a reason for
        those is the failure this module exists to avoid."""
        profile = make_profile(courses=[("BIO 101", "Bio")], declared_major=None, interests=[])
        alumnus = make_alumnus(
            courses=[("FIN 310", "Corp Fin", 0)],
            pivot_semester=None,
            final_major=None,
            origin_major="Economics",
            interests=[],
        )
        assert explain_match(_scored(profile, alumnus), alumnus, profile) is None

    def test_caps_the_number_of_clauses(self):
        profile = make_profile(
            courses=[("BIO 101", "Bio 101")],
            declared_major="Biochemistry",
            interests=["Research"],
        )
        profile.current_majors = {"Biochemistry"}
        alumnus = make_alumnus(
            courses=[("BIO 101", "Bio 101", 0)],
            pivot_semester=2,
            interests=["Research"],
        )
        assert len(_reason(profile, alumnus).factors) <= MAX_FACTORS

    def test_summary_is_the_joined_factors(self):
        profile = make_profile(courses=[("BIO 101", "Bio 101")], declared_major=None)
        alumnus = make_alumnus(courses=[("BIO 101", "Bio 101", 0)], pivot_semester=2)
        reason = _reason(profile, alumnus)
        for factor in reason.factors:
            assert factor.lower()[:12] in reason.summary.lower()

    def test_is_deterministic(self):
        profile = make_profile(courses=[("BIO 101", "Bio 101")], declared_major="Biochemistry")
        profile.current_majors = {"Biochemistry"}
        alumnus = make_alumnus(courses=[("BIO 101", "Bio 101", 0)], pivot_semester=2)
        assert _reason(profile, alumnus) == _reason(profile, alumnus)

    def test_no_reason_without_a_score(self):
        profile = make_profile()
        assert explain_match(None, make_alumnus(), profile) is None


class TestUntitledCourses:
    """MIDFIELD carries courses with blank names — naming those would render
    'shared , , and '. Seen on the real corpus, not in any fixture."""

    def test_falls_back_to_a_count_when_every_name_is_blank(self):
        blanks = [("C0", ""), ("C1", "   "), ("C2", "")]
        profile = make_profile(courses=blanks, declared_major=None)
        alumnus = make_alumnus(
            courses=[(c, n, 0) for c, n in blanks], pivot_semester=None, final_major=None
        )
        summary = _reason(profile, alumnus).summary
        assert summary == "Shared 3 courses"

    def test_named_courses_still_account_for_unnamed_ones(self):
        courses = [("C0", "Genetics"), ("C1", ""), ("C2", "")]
        profile = make_profile(courses=courses, declared_major=None)
        alumnus = make_alumnus(
            courses=[(c, n, 0) for c, n in courses], pivot_semester=None, final_major=None
        )
        summary = _reason(profile, alumnus).summary
        assert "Genetics" in summary
        assert "2 more courses" in summary

    def test_singular_course_reads_correctly(self):
        profile = make_profile(courses=[("C0", "")], declared_major=None)
        alumnus = make_alumnus(
            courses=[("C0", "", 0)], pivot_semester=None, final_major=None
        )
        assert _reason(profile, alumnus).summary == "Shared 1 course"
