"""GET /api/constellation — the primary payload behind the constellation map."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, repository
from app.auth import current_student
from app.config import settings
from app.db import get_session
from app.jobs.recompute import query_hash
from app.matching import StudentProfile, build_constellation, filter_by_pivot_query
from app.models import Student
from app.schemas import ConstellationResponse

router = APIRouter(prefix="/api", tags=["constellation"])


@router.get("/constellation", response_model=ConstellationResponse, response_model_by_alias=True)
async def get_constellation(
    to_major: str | None = Query(default=None, alias="toMajor"),
    from_major: str | None = Query(default=None, alias="fromMajor"),
    max_alumni: int | None = Query(default=None, alias="maxAlumni", ge=1, le=1000),
    refresh: bool = Query(default=False, description="Bypass the cache and recompute"),
    student: Student = Depends(current_student),
    session: AsyncSession = Depends(get_session),
) -> ConstellationResponse:
    """Serve the authenticated student's constellation, cached.

    On a hit this returns Redis's copy untouched. On a miss it computes inline
    and backfills the cache, so a cold or unavailable Redis costs latency rather
    than availability.

    There is no `studentId` parameter — the subject is the token holder, so the
    cache key is derived from an identity the caller cannot choose.
    """
    student_id = student.id
    limit = max_alumni or settings.constellation_max_alumni
    key = cache.constellation_key(student_id, query_hash(from_major, to_major, limit))

    if not refresh:
        try:
            cached_payload = await cache.get_json(key)
        except Exception:
            cached_payload = None  # Redis down — fall through and compute.
        if cached_payload is not None:
            cached_payload.setdefault("meta", {})["cached"] = True
            return ConstellationResponse.model_validate(cached_payload)

    # Only this student's own school. The corpus is the tenant boundary: an
    # alumnus from another school can never reach the response, scored or not.
    alumni = await repository.list_alumni(session, school_id=student.school_id)
    if to_major:
        alumni = filter_by_pivot_query(alumni, from_major, to_major)

    profile = StudentProfile.from_model(student)
    if from_major:
        profile.declared_major = from_major
        profile.current_majors = {from_major}
    if to_major:
        profile.intended_direction = to_major

    response, _ = build_constellation(profile, alumni, max_alumni=limit)

    try:
        await cache.set_json(key, response.model_dump(by_alias=True))
        await cache.track_student_key(student_id, key)
    except Exception:
        pass  # Caching is an optimization; never fail the request over it.

    return response
