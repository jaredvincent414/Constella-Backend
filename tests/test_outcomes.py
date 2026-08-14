"""Tests for career-outcome clustering and the synthetic outcome seed."""

from __future__ import annotations

from app.matching.clustering import build_clusters
from app.matching.outcomes import build_outcome, outcome_industry
from app.matching.scoring import score_corpus
from scripts.seed_outcomes import outcome_for
from tests.factories import make_alumnus, make_profile


def _scored(alumni):
    return score_corpus(make_profile(declared_major=None), alumni)


class TestOutcomeAccessors:
    def test_industry_prefers_the_outcome_over_the_academic_area(self):
        a = make_alumnus("a", career_area="Engineering", industry="Technology")
        assert outcome_industry(a) == "Technology"

    def test_industry_falls_back_to_career_area_without_an_outcome(self):
        a = make_alumnus("a", career_area="Engineering")  # no industry
        assert outcome_industry(a) == "Engineering"

    def test_build_outcome_uses_occupation_and_flags_provenance(self):
        a = make_alumnus("a", industry="Healthcare", occupation="Clinician")
        out = build_outcome(a)
        assert out.title == "Clinician"
        assert out.industry == "Healthcare"
        assert out.provenance == "synthetic"

    def test_build_outcome_falls_back_to_stored_title_without_data(self):
        a = make_alumnus("a")  # no outcome
        out = build_outcome(a)
        assert out.title == "Analyst"  # the factory's stored outcome_title
        assert out.provenance is None


class TestOutcomeClustering:
    def test_clusters_by_industry_not_academic_area(self):
        # Same academic area (Engineering), different industries -> two clusters.
        alumni = [
            make_alumnus("a", career_area="Engineering", industry="Technology"),
            make_alumnus("b", career_area="Engineering", industry="Aerospace & Defense"),
            make_alumnus("c", career_area="Engineering", industry="Technology"),
        ]
        clusters = {c.label: c for c in build_clusters(_scored(alumni))}
        assert set(clusters) == {"Technology", "Aerospace & Defense"}
        assert clusters["Technology"].member_count == 2

    def test_falls_back_to_academic_area_when_outcomes_absent(self):
        # No outcomes -> behaves exactly like the old career-area clustering.
        alumni = [
            make_alumnus("a", career_area="Health Policy"),
            make_alumnus("b", career_area="Biotech"),
        ]
        labels = {c.label for c in build_clusters(_scored(alumni))}
        assert labels == {"Health Policy", "Biotech"}


class TestSeedDeterminism:
    def test_same_alumnus_always_gets_the_same_outcome(self):
        assert outcome_for("MCID1", "Engineering") == outcome_for("MCID1", "Engineering")

    def test_outcome_maps_into_the_declared_field(self):
        industry, occupation, region, years = outcome_for("MCID1", "Engineering")
        engineering_industries = {"Technology", "Manufacturing", "Aerospace & Defense", "Energy"}
        assert industry in engineering_industries
        assert 1 <= years <= 6
        assert region

    def test_unknown_area_uses_the_default_pool(self):
        industry, *_ = outcome_for("MCID9", "Basket Weaving")
        assert industry in {"Professional Services", "Technology", "Nonprofit"}
