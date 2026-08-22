"""Tests for corpus intelligence — app/matching/insights.py.

Pure functions, no Postgres needed. Uses the same factories as the scoring tests.
"""

from app.matching.corpus import build_corpus
from app.matching.insights import build_insights
from tests.factories import make_alumnus, make_profile


def _corpus_with_transitions():
    """A small corpus with known transition patterns."""
    alumni = [
        make_alumnus(
            alumnus_id="a1",
            origin_major="Biology",
            final_major="Health Policy",
            career_area="Healthcare",
            pivot_semester=3,
            courses=[("BIO101", "Intro Bio", 0), ("CHEM101", "Intro Chem", 1)],
        ),
        make_alumnus(
            alumnus_id="a2",
            origin_major="Biology",
            final_major="Health Policy",
            career_area="Healthcare",
            pivot_semester=4,
            courses=[("BIO101", "Intro Bio", 0), ("PSY101", "Intro Psych", 1)],
        ),
        make_alumnus(
            alumnus_id="a3",
            origin_major="Biology",
            final_major="Computer Science",
            career_area="Technology",
            pivot_semester=5,
            courses=[("BIO101", "Intro Bio", 0), ("CS101", "Intro CS", 2)],
        ),
        make_alumnus(
            alumnus_id="a4",
            origin_major="Chemistry",
            final_major="Engineering",
            career_area="Engineering",
            pivot_semester=2,
            courses=[("CHEM101", "Intro Chem", 0), ("PHYS101", "Physics", 1)],
        ),
        make_alumnus(
            alumnus_id="a5",
            origin_major="Biology",
            career_area="Research",
            final_major=None,
            pivot_semester=None,
            courses=[("BIO101", "Intro Bio", 0), ("BIO201", "Adv Bio", 2)],
        ),
    ]
    return build_corpus(alumni, school_id="test-school")


def test_transitions_from_student_major():
    corpus = _corpus_with_transitions()
    profile = make_profile(declared_major="Biology", courses=[("BIO101", "Intro Bio")])
    insights = build_insights(profile, corpus)

    # 4 alumni started in Biology; Biology->Health Policy is the most common (2)
    assert insights.cohort_size == 4
    assert len(insights.common_transitions) > 0
    assert insights.common_transitions[0].from_major == "Biology"
    assert insights.common_transitions[0].to_major == "Health Policy"
    assert insights.common_transitions[0].count == 2


def test_outcome_distribution():
    corpus = _corpus_with_transitions()
    profile = make_profile(declared_major="Biology", courses=[("BIO101", "Intro Bio")])
    insights = build_insights(profile, corpus)

    areas = {o.career_area for o in insights.outcome_distribution}
    assert "Healthcare" in areas
    # Percentages should sum to ~100
    total_pct = sum(o.percent for o in insights.outcome_distribution)
    assert 99.0 <= total_pct <= 101.0


def test_course_signals():
    corpus = _corpus_with_transitions()
    profile = make_profile(
        declared_major="Biology",
        courses=[("BIO101", "Intro Bio"), ("CHEM101", "Intro Chem")],
    )
    insights = build_insights(profile, corpus)

    # BIO101 is shared by 3 alumni in the cohort, CHEM101 by 1
    codes = [s.course_code for s in insights.course_signals]
    assert len(codes) > 0
    # BIO101 should rank first (more alumni share it)
    assert codes[0] == "bio101"


def test_pivot_timing():
    corpus = _corpus_with_transitions()
    profile = make_profile(declared_major="Biology")
    insights = build_insights(profile, corpus)

    assert insights.pivot_timing is not None
    assert "year" in insights.pivot_timing


def test_no_major_uses_full_corpus():
    corpus = _corpus_with_transitions()
    profile = make_profile(declared_major=None)
    insights = build_insights(profile, corpus)

    # With no declared major, the cohort is the full corpus
    assert insights.cohort_size == 5


def test_empty_corpus():
    corpus = build_corpus([], school_id="empty")
    profile = make_profile(declared_major="Biology")
    insights = build_insights(profile, corpus)

    assert insights.cohort_size == 0
    assert insights.common_transitions == []
    assert insights.outcome_distribution == []
    assert insights.course_signals == []
    assert insights.pivot_timing is None
