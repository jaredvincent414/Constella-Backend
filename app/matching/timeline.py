"""Builds the semester-by-semester timeline for a single alumnus.

This is the payload behind the detail panel and the What If Simulator's
transition card. Both render the same `<AcademicTimeline>` component, so both
are served from this one function — a divergence here would show up as two
subtly different timelines for the same person.

Course status is resolved **relative to the viewing student**:

    kept    — the student has already taken this course
    added   — the alumnus took it and the student has not
    dropped — the alumnus started it and abandoned it

That framing is what makes the timeline answer the product's actual question:
not "what did this person do" but "what changes if I follow them". With no
student context (an anonymous request), nothing can be `added`, so completed
courses fall back to `kept`.
"""

from __future__ import annotations

from app.matching.scoring import ScoredAlumnus, StudentProfile
from app.matching.text import normalize_course_code
from app.models import TOTAL_SEMESTERS, Alumnus, semester_label
from app.schemas import (
    AlumnusDetail,
    OutcomeOut,
    ScoreBreakdown,
    TimelineCourse,
    TimelineSemester,
)

STATUS_KEPT = "kept"
STATUS_ADDED = "added"
STATUS_DROPPED = "dropped"


def build_semesters(
    alumnus: Alumnus,
    profile: StudentProfile | None = None,
) -> list[TimelineSemester]:
    student_codes: set[str] = set(profile.course_codes) if profile else set()

    pivot_semesters = {p.semester_index for p in alumnus.pivots}
    milestones_by_semester: dict[int, list[str]] = {}
    for milestone in alumnus.milestones:
        milestones_by_semester.setdefault(milestone.semester_index, []).append(milestone.text)

    courses_by_semester: dict[int, list[TimelineCourse]] = {}
    for course in sorted(alumnus.courses, key=lambda c: (c.semester_index, c.course_name)):
        if course.dropped:
            status = STATUS_DROPPED
        elif normalize_course_code(course.course_code) in student_codes:
            status = STATUS_KEPT
        elif profile is None:
            status = STATUS_KEPT
        else:
            status = STATUS_ADDED
        courses_by_semester.setdefault(course.semester_index, []).append(
            TimelineCourse(name=course.course_name, status=status)
        )

    populated = set(courses_by_semester) | set(milestones_by_semester) | pivot_semesters
    if not populated:
        return []

    semesters: list[TimelineSemester] = []
    for index in range(0, min(max(populated) + 1, TOTAL_SEMESTERS)):
        if index not in populated:
            # Skip gaps rather than rendering an empty row — a leave of absence
            # shouldn't show as a blank semester in the panel.
            continue
        milestone_texts = milestones_by_semester.get(index, [])
        semesters.append(
            TimelineSemester(
                label=semester_label(index),
                is_pivot=index in pivot_semesters,
                courses=courses_by_semester.get(index, []),
                milestone=" · ".join(milestone_texts) if milestone_texts else None,
            )
        )
    return semesters


def build_detail(
    scored: ScoredAlumnus | None,
    alumnus: Alumnus,
    profile: StudentProfile | None = None,
    include_breakdown: bool = True,
) -> AlumnusDetail:
    """Assemble the full detail-panel payload.

    `scored` is optional so a timeline can be fetched without a student context
    (no similarity, no breakdown).
    """
    breakdown = None
    if scored is not None and include_breakdown:
        breakdown = ScoreBreakdown(
            course_overlap=scored.course_overlap,
            pivot_year_alignment=scored.pivot_year_alignment,
            major_match=scored.major_match,
            interest_overlap=scored.interest_overlap,
            total=scored.total,
            shared_courses=scored.shared_courses,
        )

    return AlumnusDetail(
        id=alumnus.id,
        class_year=alumnus.graduation_year,
        similarity=scored.total if scored else 0.0,
        majors=alumnus.final_majors,
        outcome=OutcomeOut(title=alumnus.outcome_title, org=alumnus.outcome_org),
        interests=list(alumnus.interests or []),
        semesters=build_semesters(alumnus, profile),
        score_breakdown=breakdown,
    )
