"""Student intake — registration and the authenticated profile.

    GET  /api/students/schools   schools available to register under
    POST /api/students/register  create an account, receive a bearer token
    GET  /api/students/me        the caller's profile
    PUT  /api/students/me        partial profile update
    PUT  /api/students/me/courses  replace the transcript

Everything but `schools` and `register` requires the token. There is no route
that takes a student id: `/me` *is* the addressing scheme, which is what makes
reading another student's profile unexpressible rather than merely forbidden.

Registration is open to anyone naming a valid school. That is the honest state
of this boundary — invite codes or campus SSO belong in front of it before this
serves real students (see CLAUDE.md).
"""

from __future__ import annotations

from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.auth import current_student, new_token
from app.config import settings
from app.db import get_session
from app.jobs import recompute
from app.matching.programs import student_majors, student_minors
from app.matching.timeline import course_display_name
from app.models import ActivityKind, School, Student, semester_label
from app.schemas import (
    ActivityOut,
    CoursesUpdate,
    ProfileUpdate,
    RegisterRequest,
    RegisterResponse,
    SchoolOut,
    StudentCourseOut,
    StudentOut,
    split_name,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/students", tags=["students"])


async def _invalidate(student_id: str) -> None:
    """Drop the student's cached constellations after a profile change.

    Never fails the request: the write is already committed, so raising here
    would report failure for a change that happened and invite a client retry.
    A Redis outage costs staleness until the TTL expires, and says so in the log.
    """
    try:
        await recompute.invalidate_student_profile(student_id)
    except Exception as exc:
        log.warning("cache_invalidation_failed", student_id=student_id, error=str(exc))


def _to_out(student: Student, school: School | None) -> StudentOut:
    majors = sorted(student_majors(student))
    first_name, last_name = split_name(student.name)
    return StudentOut(
        id=student.id,
        school_id=student.school_id,
        school=school.name if school else None,
        first_name=first_name,
        last_name=last_name,
        email=student.email,
        current_year=student.year.value,
        # `student_majors` is set-valued (double majors are two rows); the profile
        # surfaces the primary declaration, so take the first of the sorted set.
        declared_major=majors[0] if majors else None,
        minors=sorted(student_minors(student)),
        intended_direction=student.intended_direction,
        interests=list(student.interests or []),
        courses_completed=[
            StudentCourseOut(
                id=c.course_code,
                # Same fallback the alumni timeline uses: a blank title would
                # render as an empty pill, and the code is never blank.
                name=course_display_name(c),
                semester=semester_label(c.semester_index),
                semester_index=c.semester_index,
            )
            for c in sorted(student.courses, key=lambda c: (c.semester_index, c.course_code))
        ],
    )


async def _out_for(session: AsyncSession, student: Student) -> StudentOut:
    school = (
        await repository.get_school(session, student.school_id) if student.school_id else None
    )
    return _to_out(student, school)


@router.get("/schools", response_model=list[SchoolOut], response_model_by_alias=True)
async def list_schools(session: AsyncSession = Depends(get_session)) -> list[SchoolOut]:
    """Schools a student can register under. Public — a signup form needs it —
    and it exposes nothing but the slug and display name."""
    return [SchoolOut(id=s.id, name=s.name) for s in await repository.list_schools(session)]


@router.post(
    "/register", response_model=RegisterResponse, response_model_by_alias=True, status_code=201
)
async def register(
    request: RegisterRequest,
    session: AsyncSession = Depends(get_session),
) -> RegisterResponse:
    """Create a student and issue their bearer token.

    The token is returned once and stored only as a SHA-256 hash.
    """
    school = await repository.get_school(session, request.school_id)
    if school is None:
        raise HTTPException(status_code=404, detail=f"School {request.school_id!r} not found")

    if await repository.get_student_by_email(session, request.email) is not None:
        # 409 rather than a silent re-issue: handing a token to whoever re-posts a
        # known address would make registration an account-takeover primitive.
        raise HTTPException(status_code=409, detail="That email is already registered")

    token, token_hash = new_token()
    student = await repository.create_student(
        session,
        student_id=f"stu-{uuid4().hex[:16]}",
        school_id=school.id,
        name=request.name,
        email=request.email,
        year=request.year,
        auth_token_hash=token_hash,
    )
    return RegisterResponse(token=token, student=_to_out(student, school))


@router.get("/me", response_model=StudentOut, response_model_by_alias=True)
async def get_me(
    student: Student = Depends(current_student),
    session: AsyncSession = Depends(get_session),
) -> StudentOut:
    return await _out_for(session, student)


@router.put("/me", response_model=StudentOut, response_model_by_alias=True)
async def update_me(
    request: ProfileUpdate,
    student: Student = Depends(current_student),
    session: AsyncSession = Depends(get_session),
) -> StudentOut:
    """Update year, major, intended direction, or interests.

    `exclude_unset` is what separates "leave this alone" from "clear this" — a
    plain dump would blank every field the client didn't send.
    """
    updates = request.model_dump(exclude_unset=True)
    updated = await repository.update_student_profile(session, student, updates)
    await repository.record_activity(
        session, student.id, ActivityKind.updated_profile, "Updated your profile"
    )
    # Every one of these fields feeds the score, so the cached constellation is
    # now wrong. Drop it rather than serve a result computed for the old profile.
    await _invalidate(student.id)
    return await _out_for(session, updated)


@router.get("/me/activity", response_model=list[ActivityOut], response_model_by_alias=True)
async def get_my_activity(
    limit: int = Query(default=4, ge=1, le=50),
    student: Student = Depends(current_student),
    session: AsyncSession = Depends(get_session),
) -> list[ActivityOut]:
    """The caller's recent actions, newest first.

    Under `/me` like everything else here — there is no route that takes a
    student id, which is what makes reading someone else's feed unexpressible
    rather than merely denied.
    """
    rows = await repository.list_activity(
        session, student.id, min(limit, settings.activity_feed_limit)
    )
    return [
        ActivityOut(id=r.id, kind=r.kind.value, label=r.label, at=r.created_at.isoformat())
        for r in rows
    ]


@router.put("/me/courses", response_model=StudentOut, response_model_by_alias=True)
async def update_my_courses(
    request: CoursesUpdate,
    student: Student = Depends(current_student),
    session: AsyncSession = Depends(get_session),
) -> StudentOut:
    """Replace the caller's transcript wholesale."""
    updated = await repository.replace_student_courses(
        session,
        student,
        [(c.code, c.name, c.semester_index) for c in request.courses],
    )
    await repository.record_activity(
        session,
        student.id,
        ActivityKind.updated_courses,
        f"Updated your transcript ({len(request.courses)} courses)",
    )
    await _invalidate(student.id)
    return await _out_for(session, updated)
