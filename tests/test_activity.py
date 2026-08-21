"""The Dashboard's recent-activity feed.

The label is the whole product here: a feed of "Explored" repeated four times
is worse than no feed. These cover what goes in the line and what keeps a burst
of identical actions from filling it.
"""

from __future__ import annotations

from app.api.routes.constellation import exploration_label
from app.jobs.recompute import ExploreQuery


def _query(**kwargs) -> ExploreQuery:
    return ExploreQuery.build(max_alumni=200, **kwargs)


class TestExplorationLabel:
    def test_a_pivot_query_reads_as_a_move(self):
        assert exploration_label(_query(to_major="Public Health")) == (
            "Explored a move to Public Health"
        )

    def test_a_career_area_names_the_cluster(self):
        assert exploration_label(_query(career_area="Health Policy")) == (
            "Explored the Health Policy cluster"
        )

    def test_a_major_names_the_paths(self):
        assert exploration_label(_query(major="Biology")) == "Explored Biology paths"

    def test_interests_are_listed(self):
        assert exploration_label(_query(interests=["Biology", "Debate"])) == (
            "Explored interests: Biology, Debate"
        )

    def test_the_most_specific_facet_wins(self):
        """One clause is what makes a feed skimmable. Concatenating every facet
        set would produce a query string, not a memory."""
        label = exploration_label(
            _query(to_major="Public Health", career_area="Health Policy", major="Biology")
        )
        assert label == "Explored a move to Public Health"

    def test_a_broad_query_still_has_a_label(self):
        # Broad queries aren't logged, but the label must not depend on that —
        # a caller that logs one should get a sentence, not an empty string.
        assert exploration_label(_query()) == "Explored the constellation"

    def test_a_long_facet_is_truncated_to_the_column(self):
        """`label` is String(200); free text arrives from a query parameter."""
        assert len(exploration_label(_query(career_area="x" * 400))) <= 200
