"""Explore facets — the filters behind the interests / career area / major controls.

These narrow the corpus; they never touch the score. That distinction is the
point of the module, and the last test in this file is what keeps it true: if a
facet ever starts nudging a score, the ranking of whoever survives it changes,
and that is a scoring change requiring an eval run rather than a filter.
"""

from __future__ import annotations

from app.matching.corpus import build_corpus
from app.matching.facets import filter_by_facets, parse_interests
from app.matching.scoring import score_corpus
from tests.factories import make_alumnus, make_profile


def _corpus():
    return build_corpus(
        [
            make_alumnus(
                "bio-health",
                career_area="Health Policy",
                origin_major="Biology",
                final_major="Public Health",
                interests=["Global Health Club", "Debate"],
            ),
            make_alumnus(
                "cs-software",
                career_area="Software",
                origin_major="Computer Science",
                final_major="Computer Science",
                pivot_semester=None,
                interests=["Robotics"],
            ),
            make_alumnus(
                "econ-health",
                career_area="Health Policy",
                origin_major="Economics",
                final_major="Public Health",
                interests=["Debate"],
            ),
        ],
        school_id="school-a",
    )


def _ids(corpus) -> set[str]:
    return {view.id for view in corpus.views}


class TestNoFacets:
    def test_returns_the_same_corpus_object(self):
        """A broad explore must pay nothing for the feature existing."""
        corpus = _corpus()
        assert filter_by_facets(corpus) is corpus
        assert filter_by_facets(corpus, [], None, None) is corpus


class TestCareerArea:
    def test_keeps_only_that_area(self):
        assert _ids(filter_by_facets(_corpus(), career_area="Health Policy")) == {
            "bio-health",
            "econ-health",
        }

    def test_matching_is_fuzzy(self):
        """These arrive as free text from a dropdown built off the corpus, so
        "Health" has to reach "Health Policy" the way the scorer's does."""
        assert "bio-health" in _ids(filter_by_facets(_corpus(), career_area="Health"))

    def test_an_unmatched_area_returns_nothing(self):
        assert _ids(filter_by_facets(_corpus(), career_area="Marine Biology")) == set()

    def test_does_not_match_on_a_major_name(self):
        """A career-area facet asks where someone ended up, not what they
        studied. On the real corpus, matching majors made "Aerospace & Defense"
        return 65 alumni of whom only 50 worked in it — Aerospace Engineering
        graduates now in Manufacturing share a token with the industry name.
        """
        corpus = build_corpus(
            [
                make_alumnus(
                    "aero-in-manufacturing",
                    career_area="Engineering",
                    final_major="Aerospace Engineering",
                    industry="Manufacturing",
                ),
                make_alumnus(
                    "actually-aero",
                    career_area="Engineering",
                    final_major="Mechanical Engineering",
                    industry="Aerospace & Defense",
                ),
            ]
        )
        assert _ids(filter_by_facets(corpus, career_area="Aerospace & Defense")) == {
            "actually-aero"
        }


class TestMajor:
    def test_matches_the_major_they_graduated_in(self):
        assert _ids(filter_by_facets(_corpus(), major="Public Health")) == {
            "bio-health",
            "econ-health",
        }

    def test_matches_the_major_they_started_in(self):
        """Filtering on Biology has to surface the student who *left* Biology.

        The pivot away is the interesting part — excluding origin majors would
        hide exactly the trajectories this product exists to show.
        """
        assert _ids(filter_by_facets(_corpus(), major="Biology")) == {"bio-health"}


class TestInterests:
    def test_any_selected_interest_qualifies(self):
        """Multi-select chips read as "any of these" to a user, and requiring
        all of them returns nothing on a corpus where interests are sparse."""
        assert _ids(filter_by_facets(_corpus(), interests=["Robotics", "Global Health"])) == {
            "bio-health",
            "cs-software",
        }

    def test_matches_on_tokens_not_exact_strings(self):
        assert _ids(filter_by_facets(_corpus(), interests=["health"])) == {"bio-health"}

    def test_an_empty_list_is_not_a_filter(self):
        assert _ids(filter_by_facets(_corpus(), interests=[])) == _ids(_corpus())


class TestFacetsCombine:
    def test_with_and(self):
        """Each facet the student set is a constraint they expect to hold."""
        kept = filter_by_facets(_corpus(), interests=["Debate"], career_area="Health Policy")
        assert _ids(kept) == {"bio-health", "econ-health"}

        narrowed = filter_by_facets(
            _corpus(), interests=["Debate"], career_area="Health Policy", major="Economics"
        )
        assert _ids(narrowed) == {"econ-health"}

    def test_contradictory_facets_return_an_empty_corpus(self):
        kept = filter_by_facets(_corpus(), career_area="Software", major="Public Health")
        assert _ids(kept) == set()
        assert kept.school_id == "school-a"


class TestFiltersDoNotScore:
    def test_scores_are_unchanged_by_filtering(self):
        """A facet decides who is eligible; the formula ranks whoever survives.

        Folding a facet into the score would let a strong course overlap outvote
        an explicit "show me Health Policy" — and would make adding one a
        scoring change, which needs an eval run rather than a filter.
        """
        profile = make_profile()
        corpus = _corpus()
        unfiltered = {s.alumnus.id: s.total for s in score_corpus(profile, corpus)}

        filtered = filter_by_facets(corpus, career_area="Health Policy")
        for scored in score_corpus(profile, filtered):
            assert scored.total == unfiltered[scored.alumnus.id]


class TestParseInterests:
    def test_splits_and_strips(self):
        assert parse_interests("Biology, Psychology") == ["Biology", "Psychology"]

    def test_drops_empties(self):
        assert parse_interests("Biology,,  ,Psychology") == ["Biology", "Psychology"]

    def test_none_and_blank_are_no_filter(self):
        assert parse_interests(None) == []
        assert parse_interests("   ") == []
