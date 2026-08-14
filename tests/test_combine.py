"""Tests for path combining — pure, no database."""

from __future__ import annotations

import pytest

from app.matching.combine import OUTCOME_STAGE, combine_paths
from tests.factories import make_alumnus, make_profile

# Two paths that share Stats I (freshman) and Research Methods (sophomore).
PATH_A = dict(
    alumnus_id="a",
    career_area="Health Policy",
    final_major="Public Health",
    pivot_semester=None,
    courses=[
        ("BIO101", "Bio 101", 0),
        ("STAT1", "Stats I", 0),
        ("ORGCHEM", "Organic Chem", 2),
        ("RESM", "Research Methods", 2),
        ("EPI", "Epidemiology", 4),
        ("COMMH", "Community Health", 6),
    ],
)
PATH_B = dict(
    alumnus_id="b",
    career_area="UX Research",
    final_major="Cognitive Science",
    pivot_semester=None,
    courses=[
        ("STAT1", "Stats I", 0),
        ("PSYC", "Intro Psych", 0),
        ("RESM", "Research Methods", 2),
        ("HCI", "Human-Computer Interaction", 4),
        ("CAP", "Capstone", 6),
    ],
)


def _combine():
    a = make_alumnus(**PATH_A)
    b = make_alumnus(**PATH_B)
    return combine_paths(make_profile(declared_major=None), [a, b])


def test_semesters_grouped_by_year_stage_shared_ranked_first():
    r = _combine()
    assert set(r.combined_path.semesters) == {"Freshman", "Sophomore", "Junior", "Senior"}
    # Stats I is shared (freq 2) so it leads the Freshman stage.
    assert r.combined_path.semesters["Freshman"][0] == "Stats I"
    assert "Research Methods" in r.combined_path.semesters["Sophomore"]


def test_shared_courses_and_confidence():
    r = _combine()
    assert r.shared_courses == ["Research Methods", "Stats I"]
    # 2 shared of 9 total-unique courses.
    assert r.combined_path.confidence == pytest.approx(round(2 / 9, 2))


def test_outcome_fields_are_distinct_career_areas():
    r = _combine()
    assert r.combined_path.outcome_fields == ["Health Policy", "UX Research"]


def test_source_paths_carry_color_index_and_major():
    r = _combine()
    assert [s.color_index for s in r.source_paths] == [0, 1]
    assert r.source_paths[0].alumnus_id == "a"
    assert r.source_paths[0].major == "Public Health"


def test_per_stage_cap_is_respected():
    # Six courses in one freshman stage, cap defaults to 4.
    crowded = make_alumnus(
        alumnus_id="c",
        pivot_semester=None,
        final_major=None,
        courses=[(f"C{i}", f"Course {i}", 0) for i in range(6)],
    )
    other = make_alumnus(alumnus_id="d", pivot_semester=None, final_major=None,
                         courses=[("C0", "Course 0", 0)])
    r = combine_paths(make_profile(declared_major=None), [crowded, other])
    assert len(r.combined_path.semesters["Freshman"]) == 4


class TestSankey:
    def test_one_link_per_path_per_transition_plus_outcome(self):
        r = _combine()
        # Each path spans 4 stages -> 3 transitions + 1 outcome link = 4; two paths.
        assert len(r.sankey.links) == 8
        assert {link.path_id for link in r.sankey.links} == {0, 1}

    def test_shared_freshman_node_lists_both_paths(self):
        r = _combine()
        node = next(n for n in r.sankey.nodes if n.id == "0-Stats I")
        assert node.stage == 0
        assert node.path_ids == [0, 1]

    def test_outcome_nodes_sit_past_the_last_stage(self):
        r = _combine()
        outcomes = [n for n in r.sankey.nodes if n.stage == OUTCOME_STAGE]
        assert {n.label for n in outcomes} == {"Health Policy", "UX Research"}

    def test_every_link_endpoint_is_a_declared_node(self):
        r = _combine()
        node_ids = {n.id for n in r.sankey.nodes}
        for link in r.sankey.links:
            assert link.source in node_ids
            assert link.target in node_ids
