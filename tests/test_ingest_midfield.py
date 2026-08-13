"""Unit tests for the MIDFIELD transform — pure, no files or database."""

from __future__ import annotations

from app.ingest.cip import career_area_for_cip, clean_degree_name
from app.ingest.sources.midfield import (
    build_semester_map,
    is_dropped,
    transform_alumnus,
)

CIP_NAME = {
    "140102": "General Engineering",
    "141001": "Electrical Engineering",
    "520201": "Business Administration",
}


def test_semester_map_primary_terms_advance_summer_shares():
    # 19881 Fall, 19883 Spring advance; 19895 summer shares the prior index.
    mapping = build_semester_map(["19881", "19883", "19895", "19911"])
    assert mapping["19881"] == 0
    assert mapping["19883"] == 1
    assert mapping["19895"] == 1  # summer folds onto Spring
    assert mapping["19911"] == 2


def test_is_dropped_only_withdrawals():
    assert is_dropped("W")
    assert is_dropped("WF")
    assert not is_dropped("A")
    assert not is_dropped("I")  # incomplete is not a withdrawal
    assert not is_dropped("")


def test_career_area_from_cip_family():
    assert career_area_for_cip("141001") == "Engineering"
    assert career_area_for_cip("520201") == "Business & Management"
    assert career_area_for_cip("") == "Other"
    assert career_area_for_cip("999999") == "Other"


def test_clean_degree_name_strips_credential_prefix():
    assert clean_degree_name("Bachelor of Science in Physics") == "Physics"
    assert clean_degree_name("Bachelor of Arts in Sociology") == "Sociology"


def test_transform_detects_pivot_and_final_major():
    degrees = [
        ("141001", "19921", "Institution J", "Bachelor of Science in Electrical Engineering"),
    ]
    # Started in General Engineering, moved into EE in the second primary term.
    terms = [
        ("19881", "140102", "01 First-year", "No"),
        ("19883", "141001", "01 First-year", "No"),
        ("19911", "141001", "03 Third-year", "Yes"),
    ]
    courses = [
        # (term, code, name, grade, discipline, hours)
        ("19881", "MATH101", "Calculus I", "A", "Mathematics", "3.0"),
        ("19883", "ECEN2230", "Microprocessor Lab", "C", "Engineering: Electrical", "1.0"),
        ("19883", "ECEN2230", "Microprocessor Lab", "W", "Engineering: Electrical", "1.0"),
    ]

    rec = transform_alumnus("MCID1", degrees, terms, courses, CIP_NAME)

    assert rec.id == "MCID1"
    assert rec.graduation_year == 1992
    assert rec.career_area == "Engineering"
    assert rec.outcome_org == "Institution J"

    # One pivot, from general to electrical, at semester index 1.
    assert len(rec.pivots) == 1
    pivot = rec.pivots[0]
    assert pivot.from_major == "General Engineering"
    assert pivot.to_major == "Electrical Engineering"
    assert pivot.semester_index == 1

    # EE is the awarded degree -> final; general engineering is not.
    finals = {m.name for m in rec.majors if m.is_final}
    assert "Electrical Engineering" in finals
    assert "General Engineering" not in finals

    # The passing retake supersedes the withdrawal.
    lab = next(c for c in rec.courses if c.code == "ECEN2230")
    assert lab.dropped is False

    # A co-op term becomes a milestone; graduation is always present.
    assert any("Co-op" in m.text for m in rec.milestones)
    assert any("Graduated" in m.text for m in rec.milestones)


def test_transform_no_pivot_when_program_stable():
    degrees = [
        ("520201", "20161", "Institution B", "Bachelor of Science in Business Administration"),
    ]
    terms = [
        ("20121", "520201", "01 First-year", "No"),
        ("20123", "520201", "02 Second-year", "No"),
    ]
    rec = transform_alumnus("MCID2", degrees, terms, [], CIP_NAME)
    assert rec.pivots == []
    assert {m.name for m in rec.majors} == {"Business Administration"}
