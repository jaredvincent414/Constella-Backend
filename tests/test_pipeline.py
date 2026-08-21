"""Tests for the assembled constellation payload and the timeline builder."""

from __future__ import annotations

from app.matching import build_constellation, build_semesters, filter_by_pivot_query
from tests.factories import make_alumnus, make_profile


def _all_alumni(response) -> list:
    return [a for cluster in response.clusters for a in cluster.alumni]


class TestConstellationPayload:
    def test_matches_the_frontend_contract(self):
        alumni = [make_alumnus(f"a{i}", career_area="Health Policy") for i in range(5)]
        response, _ = build_constellation(make_profile(), alumni)
        payload = response.model_dump(by_alias=True)

        assert set(payload) == {
            "student",
            "clusters",
            "clusterEdges",
            "totalAlumni",
            "summary",
            "meta",
        }
        assert set(payload["student"]) == {"year", "interests", "courses"}
        assert set(payload["clusters"][0]) >= {"id", "label", "similarity", "topMajors", "alumni"}
        assert set(payload["clusters"][0]["alumni"][0]) >= {
            "id",
            "similarityScore",
            "graduationYear",
            "cluster",
            "majors",
            "minors",
            "careerOutcome",
            "interests",
            "pivotPoints",
        }
        # careerOutcome carries title/org plus optional employment fields
        # (industry, occupation, region, provenance) once outcomes are seeded.
        assert set(payload["clusters"][0]["alumni"][0]["careerOutcome"]) >= {"title", "org"}

    def test_alumni_are_nested_under_their_cluster(self):
        """The grouping is the layout — the frontend draws one constellation per
        cluster, so a flat list plus a clusterId would just make it regroup."""
        alumni = [make_alumnus(f"a{i}", career_area=f"Area {i}") for i in range(15)]
        response, _ = build_constellation(make_profile(), alumni, max_clusters=10)
        assert response.clusters
        for cluster in response.clusters:
            assert cluster.alumni
            assert all(a.cluster == cluster.label for a in cluster.alumni)

    def test_similarity_score_is_a_percentage(self):
        """0..1 internally, 0..100 on the wire: the frontend renders this as a
        match percentage, and one scale for both beats each side converting."""
        alumni = [make_alumnus("a", career_area="Health Policy")]
        response, _ = build_constellation(make_profile(), alumni)
        score = _all_alumni(response)[0].similarity_score
        assert 0.0 <= score <= 100.0

    def test_ships_no_geometry(self):
        """Layout is a view concern; coordinates must never leave the server."""
        alumni = [make_alumnus("a", career_area="Health Policy")]
        payload, _ = build_constellation(make_profile(), alumni)
        blob = payload.model_dump_json()
        for forbidden in ('"x"', '"y"', "radius", "angle", "position"):
            assert forbidden not in blob

    def test_respects_the_alumni_cap(self):
        alumni = [make_alumnus(f"a{i:03d}", career_area="Health Policy") for i in range(300)]
        response, _ = build_constellation(make_profile(), alumni, max_alumni=50)
        assert len(_all_alumni(response)) == 50
        assert response.total_alumni == 50
        assert response.meta.total_candidates == 300
        assert response.meta.returned == 50

    def test_total_alumni_agrees_with_the_clusters(self):
        alumni = [make_alumnus(f"a{i}", career_area="Health Policy") for i in range(7)]
        response, _ = build_constellation(make_profile(), alumni)
        assert response.total_alumni == len(_all_alumni(response))
        assert sum(len(c.alumni) for c in response.clusters) == response.total_alumni

    def test_summary_describes_the_payload(self):
        alumni = [make_alumnus(f"a{i}", career_area="Health Policy") for i in range(7)]
        response, _ = build_constellation(make_profile(), alumni)
        assert str(response.total_alumni) in response.summary
        assert str(len(response.clusters)) in response.summary

    def test_empty_corpus_is_a_valid_empty_constellation(self):
        response, _ = build_constellation(make_profile(), [])
        assert response.clusters == []
        assert response.cluster_edges == []
        assert response.total_alumni == 0
        assert response.summary == "No alumni matched this search"


class TestTimeline:
    def test_a_blank_course_name_falls_back_to_its_code(self):
        """The corpus has titles for some catalogue entries and not others;
        without the fallback the detail panel renders empty pills."""
        profile = make_profile(courses=[])
        alumnus = make_alumnus(courses=[("ENGE1005", "", 0)], pivot_semester=3)
        names = [c.name for sem in build_semesters(alumnus, profile) for c in sem.courses]
        assert names == ["ENGE1005"]

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
        assert by_name == {"Alpha": "kept", "Beta": "new", "Gamma": "dropped"}

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
