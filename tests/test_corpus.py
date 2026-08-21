"""The prepared corpus must be a pure restatement of the ORM accessors.

`build_corpus` exists only to stop the scorer re-deriving student-independent
values once per student. The moment it computes something *different* from what
the inline accessors returned, it stops being an optimization and becomes a
silent change to the ranking — so these tests pin the derived fields to their
sources, and pin scoring-through-a-corpus to scoring-through-a-list.
"""

from __future__ import annotations

from app.matching.corpus import build_corpus, build_view
from app.matching.outcomes import outcome_industry
from app.matching.programs import all_minors, origin_majors
from app.matching.programs import final_majors as final_major_set
from app.matching.scoring import filter_by_pivot_query, score_corpus
from app.matching.text import normalize_course_code, tokenize
from tests.factories import make_alumnus, make_profile


def _corpus_records() -> list:
    return [
        make_alumnus(
            "a1",
            career_area="Health Policy",
            origin_major="Biochemistry",
            final_major="Public Health",
            courses=[("BIO 101", "Bio 101", 0), ("CHEM 101", "Chem 101", 1)],
            dropped=[("ORG 210", "Organic Chem", 2)],
            minors=[("Statistics", 4)],
            interests=["Global Health Club"],
        ),
        make_alumnus(
            "a2",
            career_area="Software",
            origin_major="Computer Science",
            final_major="Computer Science",
            pivot_semester=None,
            courses=[("CS 101", "Intro CS", 0)],
        ),
        make_alumnus(
            "a3",
            career_area="Health Policy",
            origin_major="Biology",
            final_major="Public Health",
            courses=[("BIO 101", "Bio 101", 0)],
            interests=["Debate"],
        ),
    ]


class TestViewMatchesItsSources:
    def test_program_sets_come_from_the_program_accessors(self):
        for alumnus in _corpus_records():
            view = build_view(alumnus)
            assert view.origin_majors == origin_majors(alumnus)
            assert view.final_majors == final_major_set(alumnus)
            assert view.minors == all_minors(alumnus)

    def test_destinations_are_the_major_match_targets(self):
        for alumnus in _corpus_records():
            view = build_view(alumnus)
            expected = [*final_major_set(alumnus), alumnus.career_area, outcome_industry(alumnus)]
            assert sorted(view.destinations) == sorted(expected)

    def test_pre_pivot_codes_are_normalized_and_exclude_dropped(self):
        alumnus = _corpus_records()[0]
        view = build_view(alumnus)
        expected = {
            normalize_course_code(c.course_code)
            for c in alumnus.pre_pivot_courses()
            if not c.dropped
        }
        assert view.pre_pivot_codes == expected
        assert normalize_course_code("ORG 210") not in view.pre_pivot_codes

    def test_pivot_year_is_none_for_a_straight_through_path(self):
        assert build_view(make_alumnus(pivot_semester=None)).pivot_year_index is None
        alumnus = make_alumnus(pivot_semester=3)
        assert build_view(alumnus).pivot_year_index == alumnus.first_pivot.year_index

    def test_interest_tokens_are_the_union_of_tokenized_interests(self):
        alumnus = make_alumnus(interests=["Global Health Club", "Debate"])
        expected: set[str] = set()
        for interest in alumnus.interests:
            expected |= tokenize(interest)
        assert build_view(alumnus).interest_tokens == expected


class TestScoringThroughACorpus:
    def test_is_identical_to_scoring_through_a_list(self):
        """The substitution the nightly job depends on.

        The job builds one corpus per school and scores every student against
        it; the request path still hands over a list. If these two ever diverge,
        a cached constellation stops matching what the route would have served.
        """
        alumni = _corpus_records()
        corpus = build_corpus(alumni, school_id="school-a")

        for profile in (
            make_profile(),
            make_profile(declared_major="Economics", intended_direction="Public Health"),
            make_profile(interests=["global health"], courses=[]),
        ):
            from_list = score_corpus(profile, alumni)
            from_corpus = score_corpus(profile, corpus)
            assert [s.alumnus.id for s in from_list] == [s.alumnus.id for s in from_corpus]
            for left, right in zip(from_list, from_corpus, strict=True):
                assert left.total == right.total
                assert left.course_overlap == right.course_overlap
                assert left.pivot_year_alignment == right.pivot_year_alignment
                assert left.major_match == right.major_match
                assert left.interest_overlap == right.interest_overlap
                assert left.shared_courses == right.shared_courses

    def test_a_corpus_can_be_scored_repeatedly(self):
        """Reuse is the whole point — scoring must not consume the corpus."""
        corpus = build_corpus(_corpus_records())
        profile = make_profile()
        first = score_corpus(profile, corpus)
        second = score_corpus(profile, corpus)
        assert [s.total for s in first] == [s.total for s in second]


class TestPivotFilterPreservesItsInputType:
    def test_a_list_in_gives_a_list_out(self):
        alumni = _corpus_records()
        kept = filter_by_pivot_query(alumni, None, "Public Health")
        assert isinstance(kept, list)
        assert all(hasattr(a, "graduation_year") for a in kept)

    def test_a_corpus_in_gives_a_corpus_out(self):
        """A prepared corpus has to survive the filter.

        Tearing it back down to records would make every What-If query rebuild
        the views the job just built.
        """
        corpus = build_corpus(_corpus_records(), school_id="school-a")
        kept = filter_by_pivot_query(corpus, None, "Public Health")
        assert kept.school_id == "school-a"
        assert [v.id for v in kept.views] == [
            a.id for a in filter_by_pivot_query(corpus.alumni, None, "Public Health")
        ]

    def test_no_destination_keeps_everything(self):
        corpus = build_corpus(_corpus_records())
        assert len(filter_by_pivot_query(corpus, None, None)) == 3
        assert len(filter_by_pivot_query(corpus.alumni, None, None)) == 3


class TestTokenizeIsSafeToShare:
    def test_returns_a_frozenset(self):
        """It's memoized now, so the result is shared between callers.

        A plain set would let one caller's in-place update rewrite what every
        later caller sees for the same label.
        """
        assert isinstance(tokenize("Global Health Club"), frozenset)
