"""What-If transition cards and the aggregates above them.

The card is a presenter — it must not decide anything the ranking didn't. What
these tests mostly guard is that: the timeline comes from the same builder the
detail panel uses, the outcome keeps its provenance, and the header numbers
describe the corpus rather than the page.
"""

from __future__ import annotations

from app.matching.scoring import score_corpus
from app.matching.transitions import (
    build_card,
    peak_timing,
    pre_pivot_summary,
    top_outcome,
)
from tests.factories import make_alumnus, make_profile


def _scored(alumnus, profile=None):
    return score_corpus(profile or make_profile(), [alumnus])[0]


class TestCard:
    def test_carries_the_alumnus_id(self):
        """Without it a card can't open the detail panel a node opens, and the
        two views can't reference the same person."""
        card = build_card(_scored(make_alumnus("a1")), make_profile(), is_top_match=True)
        assert card.id == "a1"

    def test_match_percent_is_a_percentage(self):
        card = build_card(_scored(make_alumnus()), make_profile(), is_top_match=True)
        assert 0.0 <= card.match_percent <= 100.0

    def test_reports_the_pivot_it_was_matched_on(self):
        alumnus = make_alumnus(
            origin_major="Economics", final_major="Public Health", pivot_semester=3
        )
        card = build_card(_scored(alumnus), make_profile(), is_top_match=True)
        assert card.from_major == "Economics"
        assert card.to_major == "Public Health"
        assert card.pivot_semester == "Sophomore Spring"

    def test_a_straight_through_path_has_no_pivot(self):
        """An alumnus can match a destination without ever having pivoted."""
        card = build_card(
            _scored(make_alumnus(pivot_semester=None)), make_profile(), is_top_match=False
        )
        assert card.from_major is None
        assert card.to_major is None
        assert card.pivot_semester is None

    def test_outcome_keeps_its_provenance(self):
        """The contract sketched a pre-joined "Title @ Org" display string.

        Employment is `provenance='synthetic'` on the placeholder dataset, and
        formatting it into a sentence strips the one field that stops the UI
        presenting invented data as reported fact.
        """
        alumnus = make_alumnus(industry="Health Policy", occupation="Analyst")
        card = build_card(_scored(alumnus), make_profile(), is_top_match=True)
        assert card.career_outcome.provenance is not None

    def test_timeline_matches_the_detail_panel(self):
        """Same builder, so a course is kept/new/dropped identically in both."""
        from app.matching import build_semesters

        profile = make_profile(courses=[("A", "Alpha")])
        alumnus = make_alumnus(
            courses=[("A", "Alpha", 0), ("B", "Beta", 1)],
            dropped=[("C", "Gamma", 2)],
            pivot_semester=3,
        )
        card = build_card(_scored(alumnus, profile), profile, is_top_match=True)
        expected = build_semesters(alumnus, profile)

        assert [s.semester for s in card.timeline] == [s.label for s in expected]
        assert [s.pivot for s in card.timeline] == [s.is_pivot for s in expected]
        flat = {c.name: c.tag for s in card.timeline for c in s.courses}
        assert flat == {"Alpha": "kept", "Beta": "new", "Gamma": "dropped"}

    def test_only_the_first_card_is_the_top_match(self):
        alumni = [make_alumnus(f"a{i}") for i in range(3)]
        profile = make_profile()
        scored = score_corpus(profile, alumni)
        cards = [build_card(s, profile, is_top_match=(i == 0)) for i, s in enumerate(scored)]
        assert [c.is_top_match for c in cards] == [True, False, False]


class TestPrePivotSummary:
    def test_counts_semesters_and_names_courses(self):
        alumnus = make_alumnus(
            courses=[("EC 101", "Economics 101", 0), ("ST 200", "Stats", 1)],
            pivot_semester=3,
        )
        summary = pre_pivot_summary(alumnus)
        assert summary.startswith("Pre-pivot (2 semesters):")
        assert "Economics 101" in summary and "Stats" in summary

    def test_truncates_a_long_transcript(self):
        alumnus = make_alumnus(
            courses=[(f"C{i}", f"Course {i}", i % 3) for i in range(10)], pivot_semester=3
        )
        assert "+7 more" in pre_pivot_summary(alumnus)

    def test_excludes_dropped_courses(self):
        """The summary describes what they completed, not what they abandoned."""
        alumnus = make_alumnus(
            courses=[("A", "Kept Course", 0)],
            dropped=[("B", "Abandoned Course", 0)],
            pivot_semester=3,
        )
        assert "Abandoned Course" not in pre_pivot_summary(alumnus)

    def test_falls_back_to_the_course_code_when_a_name_is_blank(self):
        """58% of alumnus_courses rows carry a blank course_name — the MIDFIELD
        adapter has titles for some catalogue entries and not others. Rendering
        the name directly turned the summary into a row of commas."""
        alumnus = make_alumnus(courses=[("ENGE1005", "", 0)], pivot_semester=3)
        assert "ENGE1005" in pre_pivot_summary(alumnus)

    def test_handles_an_empty_pre_pivot_transcript(self):
        alumnus = make_alumnus(courses=[], pivot_semester=0)
        assert pre_pivot_summary(alumnus) == "No coursework recorded before the pivot"


class TestPeakTiming:
    def test_names_the_modal_year(self):
        alumni = [make_alumnus(f"a{i}", pivot_semester=2) for i in range(3)]
        alumni.append(make_alumnus("late", pivot_semester=6))
        assert peak_timing(alumni) == "sophomore year"

    def test_names_both_years_on_a_tie(self):
        """A transition that peaks across two years is a real pattern; picking
        one arbitrarily would hide it."""
        alumni = [make_alumnus("a", pivot_semester=2), make_alumnus("b", pivot_semester=4)]
        assert peak_timing(alumni) == "sophomore and junior year"

    def test_is_none_when_nothing_pivoted(self):
        """Naming a peak over an empty set would be an invented finding."""
        assert peak_timing([make_alumnus("a", pivot_semester=None)]) is None
        assert peak_timing([]) is None


class TestTopOutcome:
    def test_counts_the_most_common_industry(self):
        alumni = [
            make_alumnus("a", career_area="Health Policy"),
            make_alumnus("b", career_area="Health Policy"),
            make_alumnus("c", career_area="Software"),
        ]
        assert top_outcome(alumni) == ("Health Policy", 2)

    def test_falls_back_to_the_academic_area_without_employment_data(self):
        assert top_outcome([make_alumnus("a", career_area="Agriculture")]) == ("Agriculture", 1)

    def test_empty_candidates(self):
        assert top_outcome([]) == (None, 0)
