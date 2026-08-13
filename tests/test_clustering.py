"""Tests for career-outcome clustering and edge pruning."""

from __future__ import annotations

import pytest

from app.matching.clustering import build_cluster_edges, build_clusters
from app.matching.scoring import score_corpus
from tests.factories import make_alumnus, make_profile


def scored(alumni):
    return score_corpus(make_profile(declared_major=None), alumni)


class TestClusters:
    def test_groups_by_career_area(self):
        alumni = [
            make_alumnus("a", career_area="Health Policy"),
            make_alumnus("b", career_area="Health Policy"),
            make_alumnus("c", career_area="Biotech"),
        ]
        clusters = build_clusters(scored(alumni))
        by_id = {c.id: c for c in clusters}
        assert set(by_id) == {"health-policy", "biotech"}
        assert by_id["health-policy"].member_count == 2
        assert by_id["biotech"].member_count == 1

    def test_similarity_is_the_mean_not_the_max(self):
        """One strong member must not make a weak cluster look close.

        Radius is drawn from this number, so using max would put a mostly-poor
        cluster right next to the student.
        """
        profile = make_profile(courses=[("A", "Alpha")], declared_major=None)
        alumni = [
            make_alumnus("strong", courses=[("A", "Alpha", 0)], pivot_semester=2),
            make_alumnus("weak", courses=[], pivot_semester=6),
        ]
        items = score_corpus(profile, alumni)
        cluster = build_clusters(items)[0]
        expected = sum(i.total for i in items) / len(items)
        assert cluster.similarity == pytest.approx(expected, abs=1e-4)
        assert cluster.similarity < max(i.total for i in items)

    def test_label_preserves_original_casing(self):
        clusters = build_clusters(scored([make_alumnus("a", career_area="UX Research")]))
        assert clusters[0].label == "UX Research"
        assert clusters[0].id == "ux-research"

    def test_top_majors_ranked_by_frequency(self):
        alumni = [
            make_alumnus("a", final_major="Public Health"),
            make_alumnus("b", final_major="Public Health"),
            make_alumnus("c", final_major="Biology"),
        ]
        assert build_clusters(scored(alumni))[0].top_majors[0] == "Public Health"

    def test_respects_the_cluster_ceiling(self):
        """Past the layout's angular limit, the weakest clusters are dropped."""
        alumni = [make_alumnus(f"a{i}", career_area=f"Area {i}") for i in range(15)]
        clusters = build_clusters(scored(alumni), max_clusters=10)
        assert len(clusters) == 10

    def test_members_sorted_within_cluster(self):
        profile = make_profile(courses=[("A", "Alpha")], declared_major=None)
        alumni = [
            make_alumnus("weak", courses=[], pivot_semester=6),
            make_alumnus("strong", courses=[("A", "Alpha", 0)], pivot_semester=2),
        ]
        cluster = build_clusters(score_corpus(profile, alumni))[0]
        totals = [m.total for m in cluster.members]
        assert totals == sorted(totals, reverse=True)


class TestClusterEdges:
    def test_jaccard_over_major_sets(self):
        alumni = [
            make_alumnus("a", career_area="Health Policy", final_major="Biology"),
            make_alumnus("b", career_area="Biotech", final_major="Biology"),
        ]
        edges, _ = build_cluster_edges(build_clusters(scored(alumni)), 0.25, 12)
        assert len(edges) == 1
        assert edges[0].weight == 1.0

    def test_drops_edges_below_min_weight(self):
        clusters = build_clusters(
            scored(
                [
                    make_alumnus("a", career_area="X", final_major="Biology"),
                    make_alumnus("b", career_area="Y", final_major="Physics"),
                ]
            )
        )
        edges, before = build_cluster_edges(clusters, min_weight=0.25, max_count=12)
        assert edges == []
        assert before == 0  # disjoint major sets produce no candidate at all

    def test_caps_at_max_count(self):
        """Eight clusters sharing one major would otherwise hairball."""
        alumni = [
            make_alumnus(f"a{i}", career_area=f"Area {i}", final_major="Biology")
            for i in range(10)
        ]
        clusters = build_clusters(scored(alumni), max_clusters=10)
        edges, before = build_cluster_edges(clusters, min_weight=0.25, max_count=12)
        assert before == 45  # complete graph on 10 nodes
        assert len(edges) == 12

    def test_sorted_by_weight_descending(self):
        alumni = [
            make_alumnus("a", career_area="X", final_major="Biology"),
            make_alumnus("b", career_area="Y", final_major="Biology"),
            make_alumnus("c", career_area="Z", final_major="Chemistry"),
            make_alumnus("d", career_area="Z", final_major="Biology"),
        ]
        edges, _ = build_cluster_edges(build_clusters(scored(alumni)), 0.0, 12)
        weights = [e.weight for e in edges]
        assert weights == sorted(weights, reverse=True)

    def test_no_self_edges(self):
        alumni = [
            make_alumnus("a", career_area="X", final_major="Biology"),
            make_alumnus("b", career_area="X", final_major="Biology"),
        ]
        edges, _ = build_cluster_edges(build_clusters(scored(alumni)), 0.0, 12)
        assert all(e.source != e.target for e in edges)
