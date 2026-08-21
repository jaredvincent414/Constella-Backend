"""GET /api/alumni/{id}/timeline — the detail panel behind a constellation node."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, repository
from app.api.responses import cached_json
from app.auth import current_student
from app.db import get_session
from app.matching import StudentProfile, build_detail, score_corpus
from app.models import Student
from app.schemas import AlumnusDetail

router = APIRouter(prefix="/api/alumni", tags=["alumni"])


@router.get("/{alumnus_id}/timeline", response_model=AlumnusDetail, response_model_by_alias=True)
async def get_timeline(
    request: Request,
    alumnus_id: str,
    student: Student = Depends(current_student),
    session: AsyncSession = Depends(get_session),
) -> AlumnusDetail | Response:
    """Full academic timeline for one alumnus at the caller's school.

    Courses resolve as kept/added relative to the caller and the response carries
    their score breakdown — the comparison is the point of the panel, so the
    student is taken from the token rather than a query parameter.

    An id from another school 404s with the same body as an id that doesn't
    exist. Distinguishing the two would confirm which alumni a school has, which
    is exactly the enumeration this boundary is meant to prevent.

    Cached per (student, alumnus): this fires on every node click, and the same
    student reopening the same node is the common case. The key carries the
    student because the payload is a comparison against *their* transcript —
    see `cache.timeline_key`.
    """
    key = cache.timeline_key(student.id, alumnus_id)
    try:
        cached_raw = await cache.get_raw(key)
    except Exception:
        cached_raw = None  # Redis down — fall through and compute.
    if cached_raw is not None:
        return cached_json(cached_raw, request)

    alumnus = await repository.get_alumnus(session, alumnus_id, school_id=student.school_id)
    if alumnus is None:
        raise HTTPException(status_code=404, detail=f"Alumnus {alumnus_id!r} not found")

    profile = StudentProfile.from_model(student)
    # Scoring a single-element corpus reuses the exact same code path as the
    # constellation, so the panel can never disagree with the node it opened.
    scored = score_corpus(profile, [alumnus])[0]
    detail = build_detail(scored, alumnus, profile)

    try:
        await cache.set_raw(key, cache.serialize_cached(detail.model_dump(by_alias=True)))
        # Tracked so a profile change drops it alongside the constellation. The
        # breakdown is scored against the student's transcript, so a stale entry
        # would show a match that no longer holds.
        await cache.track_student_key(student.id, key, {"kind": cache.KIND_TIMELINE})
    except Exception:
        pass  # Caching is an optimization; never fail the request over it.

    return detail
