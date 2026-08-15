"""In-memory model builders.

The matching engine operates on ORM objects but never touches a session, so
these construct unsaved instances and the tests run without Postgres.
"""

from __future__ import annotations

from app.matching import StudentProfile
from app.models import (
    Alumnus,
    AlumnusCourse,
    AlumnusMajor,
    CareerOutcome,
    Milestone,
    Pivot,
    ProgramRole,
    Provenance,
)


def make_profile(
    student_id: str = "s1",
    year_index: int = 1,
    declared_major: str | None = "Biochemistry",
    intended_direction: str | None = None,
    interests: list[str] | None = None,
    courses: list[tuple[str, str]] | None = None,
) -> StudentProfile:
    courses = courses or [("BIO 101", "Bio 101"), ("CHEM 101", "Chem 101")]
    from app.matching.text import normalize_course_code

    codes = [normalize_course_code(c) for c, _ in courses]
    return StudentProfile(
        id=student_id,
        year_index=year_index,
        declared_major=declared_major,
        intended_direction=intended_direction,
        interests=interests or [],
        course_codes=codes,
        course_names=dict(zip(codes, [n for _, n in courses], strict=True)),
    )


def make_alumnus(
    alumnus_id: str = "a1",
    career_area: str = "Health Policy",
    graduation_year: int = 2022,
    origin_major: str = "Biochemistry",
    final_major: str | None = "Public Health",
    pivot_semester: int | None = 3,
    courses: list[tuple[str, str, int]] | None = None,
    interests: list[str] | None = None,
    dropped: list[tuple[str, str, int]] | None = None,
    second_majors: list[tuple[str, int]] | None = None,
    minors: list[tuple[str, int]] | None = None,
    minor_provenance: str = "reported",
    industry: str | None = None,
    occupation: str = "Analyst",
    years_post_grad: int = 3,
    program_cip6: str | None = None,
) -> Alumnus:
    """Build an unsaved Alumnus.

    `program_cip6` stamps a CIP code on every program row, reproducing the shape
    real registrar exports have: CIP on the alumni side, never on the student
    side (the app writes `student_program` from free text). Leaving it None —
    the default — is what let a key-space mismatch between the two sides go
    unnoticed, so tests that touch program matching should set it.
    """
    alumnus = Alumnus(
        id=alumnus_id,
        graduation_year=graduation_year,
        outcome_title="Analyst",
        outcome_org="Agency",
        career_area=career_area,
        interests=interests or [],
    )
    for code, name, semester in courses or []:
        alumnus.courses.append(
            AlumnusCourse(course_code=code, course_name=name, semester_index=semester)
        )
    for code, name, semester in dropped or []:
        alumnus.courses.append(
            AlumnusCourse(
                course_code=code, course_name=name, semester_index=semester, dropped=True
            )
        )

    alumnus.majors.append(
        AlumnusMajor(
            name=origin_major,
            cip6=program_cip6,
            declared_semester=1,
            is_final=final_major is None,
            role=ProgramRole.primary,
            provenance=Provenance.reported,
        )
    )
    if final_major is not None:
        alumnus.majors.append(
            AlumnusMajor(
                name=final_major,
                cip6=program_cip6,
                declared_semester=pivot_semester if pivot_semester is not None else 4,
                is_final=True,
                role=ProgramRole.primary,
                provenance=Provenance.reported,
            )
        )
    for name, declared in second_majors or []:
        alumnus.majors.append(
            AlumnusMajor(
                name=name,
                cip6=program_cip6,
                declared_semester=declared,
                is_final=True,
                role=ProgramRole.second_major,
                provenance=Provenance.reported,
            )
        )
    for name, declared in minors or []:
        alumnus.majors.append(
            AlumnusMajor(
                name=name,
                cip6=program_cip6,
                declared_semester=declared,
                is_final=True,
                role=ProgramRole.minor,
                provenance=Provenance(minor_provenance),
            )
        )
    if pivot_semester is not None:
        alumnus.pivots.append(
            Pivot(
                semester_index=pivot_semester,
                from_major=origin_major,
                to_major=final_major or origin_major,
            )
        )
    if industry is not None:
        alumnus.outcomes.append(
            CareerOutcome(
                industry=industry,
                occupation=occupation,
                employer_region="Remote",
                years_post_grad=years_post_grad,
                provenance=Provenance.synthetic,
            )
        )
    alumnus.milestones.append(Milestone(semester_index=7, text="Graduated"))
    return alumnus
