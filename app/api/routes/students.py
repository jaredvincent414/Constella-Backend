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

import secrets
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, repository
from app.auth import current_student, hash_password, new_token, verify_password
from app.config import settings
from app.db import get_session
from app.jobs import recompute
from app.matching.programs import student_majors, student_minors
from app.matching.timeline import course_display_name
from app.models import ActivityKind, School, Student, semester_label
from app.schemas import (
    ActivityOut,
    CoursesUpdate,
    LoginRequest,
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


# A syntactically valid hash for a password nobody holds, so the no-such-student
# branch performs the same work the found branch does.
_ABSENT_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


async def _login_blocked(email: str, address: str) -> bool:
    """Redis down means unthrottled rather than locked out. Availability of the
    login route matters more than the throttle, which is defence in depth."""
    try:
        limit = settings.login_max_failures
        return (
            await cache.login_failures("email", email) >= limit
            or await cache.login_failures("address", address) >= limit
        )
    except Exception:
        return False


async def _record_login_failure(email: str, address: str) -> None:
    try:
        window = settings.login_failure_window_seconds
        await cache.record_login_failure("email", email, window)
        await cache.record_login_failure("address", address, window)
    except Exception:
        log.warning("login_throttle_write_failed")


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
    # Prefer the columns; fall back to splitting `name` for rows that predate
    # them and were somehow missed by the backfill.
    first_name, last_name = student.first_name, student.last_name
    if not first_name and not last_name:
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
    first_name = (request.first_name or "").strip() or None
    last_name = (request.last_name or "").strip() or None
    student = await repository.create_student(
        session,
        student_id=f"stu-{uuid4().hex[:16]}",
        school_id=school.id,
        # `name` stays the display value and the one older code reads; the two
        # columns are what the form actually collected.
        name=" ".join(p for p in (first_name, last_name) if p) or None,
        first_name=first_name,
        last_name=last_name,
        password_hash=hash_password(request.password),
        email=request.email,
        year=request.year,
        auth_token_hash=token_hash,
    )
    return RegisterResponse(token=token, student=_to_out(student, school))


@router.post("/login", response_model=RegisterResponse, response_model_by_alias=True)
async def login(
    request: Request,
    body: LoginRequest,
    session: AsyncSession = Depends(get_session),
) -> RegisterResponse:
    """Exchange email and password for a fresh bearer token.

    Every failure returns the same 401 with the same body — an unknown address,
    a wrong password, and an account that predates password auth are
    indistinguishable from outside, so this cannot be used to enumerate who has
    registered.

    Failures are counted per email and per client address; whichever trips first
    returns 429 until the window passes. The counters carry a TTL rather than
    setting a lockout flag, so an attacker failing on someone's behalf costs
    that person a wait, not their account.
    """
    address = request.client.host if request.client else "unknown"
    if await _login_blocked(body.email, address):
        raise HTTPException(
            status_code=429, detail="Too many failed attempts. Try again later."
        )

    student = await repository.get_student_by_email(session, body.email)
    # Verify even when there is no such student, against a throwaway hash, so a
    # miss and a wrong password take comparable time. Timing is a weak channel
    # here, but making it uniform costs one KDF call on a path that is already
    # deliberately slow.
    stored = student.password_hash if student else _ABSENT_PASSWORD_HASH
    if not verify_password(body.password, stored) or student is None:
        await _record_login_failure(body.email, address)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if student.school_id is None:
        # Same fail-closed rule `current_student` applies: a null tenant means
        # unscoped, so such an account must not receive a usable token.
        raise HTTPException(status_code=403, detail="Student is not associated with a school")

    token, token_hash = new_token()
    previous_hash = await repository.rotate_auth_token(session, student, token_hash)
    # The old token is dead in Postgres but its resolved principal is cached;
    # without this it keeps authenticating for the auth-cache TTL.
    for coro in (
        cache.forget_principal(previous_hash) if previous_hash else None,
        cache.clear_login_failures("email", body.email),
        cache.clear_login_failures("address", address),
    ):
        if coro is None:
            continue
        try:
            await coro
        except Exception:
            log.warning("login_cache_cleanup_failed", student_id=student.id)

    refreshed = await repository.get_student(session, student.id)
    assert refreshed is not None
    return RegisterResponse(token=token, student=await _out_for(session, refreshed))


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
