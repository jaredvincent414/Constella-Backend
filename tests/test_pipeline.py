"""Tests for the assembled constellation payload and the timeline builder."""

from __future__ import annotations

from app.matching import build_constellation, build_semesters, filter_by_pivot_query
from tests.factories import make_alumnus, make_profile


class TestConstellationPayload:
    def test_matches_the_frontend_contract(self):
        alumni = [make_alumnus(f"a{i}", career_area="Health Policy") for i in range(5)]
        response, _ = build_constellation(make_profile(), alumni)
        payload = response.model_dump(by_alias=True)

        assert set(payload) == {"student", "clusters", "alumni", "clusterEdges", "meta"}
        assert set(payload["student"]) == {"year", "interests", "courses"}
        assert set(payload["clusters"][0]) >= {"id", "label", "similarity", "topMajors"}
        assert set(payload["alumni"][0]) >= {
            "id",
            "clusterId",
            "similarity",
            "classYear",
            "majors",
            "outcome",
        }
        assert set(payload["alumni"][0]["outcome"]) == {"title", "org"}

    def test_ships_no_geometry(self):
        """Layout is a view concern; coordinates must never leave the server."""
        alumni = [make_alumnus("a", career_area="Health Policy")]
        payload, _ = build_constellation(make_profile(), alumni)
        blob = payload.model_dump_json()
        for forbidden in ('"x"', '"y"', "radius", "angle", "position"):
            assert forbidden not in blob

    def test_every_alumnus_references_a_returned_cluster(self):
        alumni = [make_alumnus(f"a{i}", career_area=f"Area {i}") for i in range(15)]
        response, _ = build_constellation(make_profile(), alumni, max_clusters=10)
        cluster_ids = {c.id for c in response.clusters}
        assert cluster_ids
        assert all(a.cluster_id in cluster_ids for a in response.alumni)

    def test_respects_the_alumni_cap(self):
        alumni = [make_alumnus(f"a{i:03d}", career_area="Health Policy") for i in range(300)]
        response, _ = build_constellation(make_profile(), alumni, max_alumni=50)
        assert len(response.alumni) == 50
        assert response.meta.total_candidates == 300
        assert response.meta.returned == 50

    def test_member_counts_agree_with_the_alumni_list(self):
        alumni = [make_alumnus(f"a{i}", career_area="Health Policy") for i in range(7)]
        response, _ = build_constellation(make_profile(), alumni)
        assert sum(c.member_count for c in response.clusters) == len(response.alumni)

    def test_empty_corpus_is_a_valid_empty_constellation(self):
        response, _ = build_constellation(make_profile(), [])
        assert response.clusters == []
        assert response.alumni == []
        assert response.cluster_edges == []


class TestTimeline:
    def test_course_status_is_relative_to_the_student(self):
        profile = make_profile(courses=[("A", "Alpha")])
        alumnus = make_alumnus(
            courses=[("A", "Alpha", 0), ("B", "Beta", 1)],
            dropped=[("C", "Gamma", 2)],
            pivot_semester=3,
        )
        by_name = {
            c.name: c.status for s in build_semesters(alumnus, profile) for c in s.courses
        }
        assert by_name == {"Alpha": "kept", "Beta": "added", "Gamma": "dropped"}

    def test_without_a_student_nothing_is_added(self):
        alumnus = make_alumnus(courses=[("A", "Alpha", 0)], pivot_semester=None, final_major=None)
        statuses = {c.status for s in build_semesters(alumnus) for c in s.courses}
        assert statuses == {"kept"}

    def test_pivot_semester_is_flagged_exactly_once(self):
        alumnus = make_alumnus(
            courses=[("A", "Alpha", i) for i in range(8)], pivot_semester=3
        )
        semesters = build_semesters(alumnus)
        assert [s.label for s in semesters if s.is_pivot] == ["Sophomore Spring"]

    def test_semester_labels(self):
        alumnus = make_alumnus(
            courses=[("A", "Alpha", 0), ("B", "Beta", 7)], pivot_semester=None, final_major=None
        )
        labels = [s.label for s in build_semesters(alumnus)]
        assert labels[0] == "Freshman Fall"
        assert labels[-1] == "Senior Spring"

    def test_gaps_are_skipped(self):
        """A semester with nothing in it is omitted, not rendered blank."""
        alumnus = make_alumnus(
            courses=[("A", "Alpha", 0), ("B", "Beta", 4)], pivot_semester=None, final_major=None
        )
        alumnus.milestones.clear()  # the factory's "Graduated" would populate semester 7
        assert [s.label for s in build_semesters(alumnus)] == ["Freshman Fall", "Junior Fall"]

    def test_milestone_only_semester_is_kept(self):
        alumnus = make_alumnus(
            courses=[("A", "Alpha", 0)], pivot_semester=None, final_major=None
        )
        semesters = build_semesters(alumnus)
        assert semesters[-1].label == "Senior Spring"
        assert semesters[-1].milestone == "Graduated"
        assert semesters[-1].courses == []


class TestPivotQueryFilter:
    def test_broad_explore_keeps_everything(self):
        alumni = [make_alumnus("a"), make_alumnus("b", career_area="Biotech")]
        assert len(filter_by_pivot_query(alumni, None, None)) == 2

    def test_narrows_to_the_requested_destination(self):
        alumni = [
            make_alumnus("health", career_area="Health Policy", final_major="Public Health"),
            make_alumnus("finance", career_area="Finance", final_major="Economics"),
        ]
        result = filter_by_pivot_query(alumni, None, "Health Policy")
        assert [a.id for a in result] == ["health"]
