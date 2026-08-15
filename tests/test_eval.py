"""Tests for the offline evaluation harness.

The metrics are pure, so they're tested against hand-built rankings rather than
a corpus. The harness pieces are tested against factory-built alumni — no
Postgres, same as the rest of the suite.
"""

from __future__ import annotations

import pytest

from app.eval.harness import (
    ablation,
    component_distribution,
    evaluate_holdout,
    held_out_profile,
)
from app.eval.metrics import (
    base_rate,
    describe,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    spearman,
)
from tests.factories import make_alumnus, make_profile


class TestDescribe:
    def test_summarizes_a_spread(self):
        d = describe([0.0, 0.5, 1.0])
        assert (d.count, d.minimum, d.median, d.maximum) == (3, 0.0, 0.5, 1.0)
        assert d.distinct == 3
        assert not d.is_constant

    def test_flags_a_constant_component(self):
        assert describe([0.25] * 50).is_constant

    def test_single_value_is_constant(self):
        assert describe([0.4]).is_constant

    def test_empty_is_safe(self):
        d = describe([])
        assert d.count == 0 and d.distinct == 0


class TestRankingMetrics:
    RANKED = ["a", "b", "c", "d", "e"]

    def test_precision_at_k(self):
        assert precision_at_k(self.RANKED, {"a", "c"}, 4) == pytest.approx(0.5)

    def test_recall_at_k(self):
        # Two of the four relevant items are in the top 3.
        assert recall_at_k(self.RANKED, {"a", "b", "x", "y"}, 3) == pytest.approx(0.5)

    def test_recall_with_no_relevant_items(self):
        assert recall_at_k(self.RANKED, set(), 3) == 0.0

    def test_reciprocal_rank_finds_the_first_hit(self):
        assert reciprocal_rank(self.RANKED, {"c", "e"}) == pytest.approx(1 / 3)

    def test_reciprocal_rank_without_a_hit(self):
        assert reciprocal_rank(self.RANKED, {"zzz"}) == 0.0

    def test_base_rate_is_the_random_baseline(self):
        assert base_rate(100, set("abcdefghij")) == pytest.approx(0.10)

    def test_perfect_ranking_beats_the_base_rate(self):
        """The property every lift number rests on."""
        ranked = [f"r{i}" for i in range(10)] + [f"x{i}" for i in range(90)]
        relevant = {f"r{i}" for i in range(10)}
        assert precision_at_k(ranked, relevant, 10) > base_rate(100, relevant)


class TestSpearman:
    def test_identical_orderings(self):
        assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)

    def test_reversed_orderings(self):
        assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_a_constant_side_correlates_perfectly(self):
        """No ordering to disagree with — must not divide by zero."""
        assert spearman([1, 2, 3], [5, 5, 5]) == pytest.approx(1.0)

    def test_handles_ties(self):
        assert spearman([1, 1, 2], [3, 3, 9]) == pytest.approx(1.0)

    def test_rejects_mismatched_lengths(self):
        with pytest.raises(ValueError):
            spearman([1, 2], [1, 2, 3])


class TestHeldOutProfile:
    def _pivoter(self):
        return make_alumnus(
            "p1",
            origin_major="Biochemistry",
            final_major="Public Health",
            pivot_semester=4,
            courses=[("BIO 101", "Bio", 0), ("CHEM 210", "Organic", 2), ("PH 320", "Policy", 6)],
            dropped=[("BIO 240", "Cell Bio", 3)],
            career_area="Health Policy",
        )

    def test_never_leaks_the_destination(self):
        """The label must not appear in the query.

        `intended_direction` is scored directly against the alumnus's
        destination, so filling it in would grade the engine on an answer it was
        handed — the metrics would look excellent and mean nothing.
        """
        profile = held_out_profile(self._pivoter())
        assert profile.intended_direction is None

    def test_uses_only_pre_pivot_courses(self):
        profile = held_out_profile(self._pivoter())
        assert "ph320" not in profile.course_codes  # taken after the pivot

    def test_excludes_dropped_courses(self):
        profile = held_out_profile(self._pivoter())
        assert "bio240" not in profile.course_codes

    def test_stands_them_at_the_pivot_year(self):
        # semester 4 -> junior (year index 2)
        assert held_out_profile(self._pivoter()).year_index == 2

    def test_uses_the_origin_major_not_the_final_one(self):
        profile = held_out_profile(self._pivoter())
        assert profile.current_majors == {"Biochemistry"}

    def test_returns_none_without_a_pivot(self):
        straight = make_alumnus("s1", final_major=None, pivot_semester=None)
        assert held_out_profile(straight) is None


class TestHoldoutEvaluation:
    def _corpus(self):
        """Ten alumni: five land in Health Policy, five in Finance. The Health
        Policy cohort shares a transcript, so course overlap should surface
        them for a Health Policy holdout."""
        corpus = []
        for i in range(5):
            corpus.append(
                make_alumnus(
                    f"hp{i}",
                    origin_major="Biochemistry",
                    final_major="Public Health",
                    pivot_semester=3,
                    career_area="Health Policy",
                    courses=[("BIO 101", "Bio", 0), ("CHEM 210", "Organic", 1)],
                )
            )
        for i in range(5):
            corpus.append(
                make_alumnus(
                    f"fin{i}",
                    origin_major="Economics",
                    final_major="Finance",
                    pivot_semester=3,
                    career_area="Finance",
                    courses=[("ECON 202", "Macro", 0), ("FIN 310", "Corp Fin", 1)],
                )
            )
        return corpus

    def test_reports_one_query_per_sampled_pivoter(self):
        report = evaluate_holdout(self._corpus(), sample_size=4, k_values=(5,))
        assert report.queries == 4

    def test_a_separable_corpus_beats_chance(self):
        report = evaluate_holdout(self._corpus(), sample_size=10, k_values=(4,))
        assert report.lift_at(4) > 1.0
        assert report.mean_reciprocal_rank > 0

    def test_holdout_never_ranks_itself(self):
        corpus = self._corpus()
        report = evaluate_holdout(corpus, sample_size=10, k_values=(4,))
        # Every query's corpus is one smaller than the whole.
        assert all(r.corpus_size == len(corpus) - 1 for r in report.results)

    def test_no_queries_when_nothing_pivots(self):
        flat = [make_alumnus(f"n{i}", final_major=None, pivot_semester=None) for i in range(3)]
        assert evaluate_holdout(flat, sample_size=3).queries == 0


class TestDiagnostics:
    def test_detects_a_dead_component(self):
        """No alumnus has interests, so interest_overlap is 0 everywhere."""
        corpus = [make_alumnus(f"a{i}", career_area=f"Area {i}") for i in range(4)]
        profiles = [make_profile(interests=[])]
        report = component_distribution(profiles, corpus)
        assert "interest_overlap" in report.dead

    def test_a_live_component_is_not_reported_dead(self):
        # Alignment is |pivot_year - student_year|, so the pivot years have to
        # differ in *distance* from the student's. A sophomore (year 1) is
        # equidistant from semesters 1 and 5, which would be constant after all.
        corpus = [
            make_alumnus("a", pivot_semester=2),  # year 1, distance 0
            make_alumnus("b", pivot_semester=6),  # year 3, distance 2
        ]
        report = component_distribution([make_profile(year_index=1)], corpus)
        assert "pivot_year_alignment" not in report.dead

    def test_ablation_marks_an_inert_component(self):
        """A component that is constant cannot reorder anything, so removing it
        leaves the ranking identical."""
        corpus = [
            make_alumnus("a", pivot_semester=1, interests=[]),
            make_alumnus("b", pivot_semester=5, interests=[]),
            make_alumnus("c", pivot_semester=7, interests=[]),
        ]
        correlations = ablation([make_profile(interests=[])], corpus)
        assert correlations["interest_overlap"] == pytest.approx(1.0)
        assert correlations["pivot_year_alignment"] < 1.0
