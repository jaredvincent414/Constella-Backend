"""GET /api/constellation — the primary payload behind the constellation map."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, repository
from app.auth import current_student
from app.config import settings
from app.db import get_session
from app.jobs.recompute import query_hash
from app.matching import StudentProfile, build_constellation_for_query
from app.models import Student
from app.schemas import ConstellationResponse

router = APIRouter(prefix="/api", tags=["constellation"])


def _json(raw: str) -> Response:
    """Hand the cache's bytes straight to the client.

    Returning a `Response` skips the `response_model` round trip — parsing the
    stored JSON into a model only for FastAPI to serialize it back would be more
    work than the Redis GET that produced it. `meta.cached` is already true in
    the stored copy (see `cache.serialize_cached`), so nothing needs patching.
    """
    return Response(content=raw, media_type="application/json")


@router.get("/constellation", response_model=ConstellationResponse, response_model_by_alias=True)
async def get_constellation(
    to_major: str | None = Query(default=None, alias="toMajor"),
    from_major: str | None = Query(default=None, alias="fromMajor"),
    max_alumni: int | None = Query(default=None, alias="maxAlumni", ge=1, le=1000),
    refresh: bool = Query(default=False, description="Bypass the cache and recompute"),
    student: Student = Depends(current_student),
    session: AsyncSession = Depends(get_session),
) -> ConstellationResponse | Response:
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
            cached_raw = await cache.get_raw(key)
        except Exception:
            cached_raw = None  # Redis down — fall through and compute.
        if cached_raw is not None:
            return _json(cached_raw)

        # Single-flight. Without it, N concurrent misses on the same key each
        # load the corpus and score it, which is exactly when the server can
        # least afford N times the work. Losing the race is not an error: the
        # loser waits briefly, then computes anyway rather than failing.
        try:
            won = await cache.acquire_compute_lock(key)
        except Exception:
            won = True
        if not won:
            try:
                waited = await cache.await_entry(key)
            except Exception:
                waited = None
            if waited is not None:
                return _json(waited)

    try:
        return await _compute_and_cache(
            session, student, key, from_major, to_major, limit
        )
    finally:
        if not refresh:
            try:
                await cache.release_compute_lock(key)
            except Exception:
                pass


async def _compute_and_cache(
    session: AsyncSession,
    student: Student,
    key: str,
    from_major: str | None,
    to_major: str | None,
    limit: int,
) -> ConstellationResponse:
    # Only this student's own school. The corpus is the tenant boundary: an
    # alumnus from another school can never reach the response, scored or not.
    alumni = await repository.list_alumni(session, school_id=student.school_id)
    profile = StudentProfile.from_model(student)

    # Scoring is synchronous, CPU-bound, and proportional to the corpus size —
    # run inline it would block the event loop for every other request this
    # worker is serving. The thread never touches the session, and the corpus is
    # fully eager-loaded, so no lazy load can fire from off-loop.
    response = await asyncio.to_thread(
        build_constellation_for_query, profile, alumni, from_major, to_major, limit
    )

    try:
        payload = response.model_dump(by_alias=True)
        await cache.set_raw(key, cache.serialize_cached(payload))
        await cache.track_student_key(
            student.id,
            key,
            {
                "kind": cache.KIND_CONSTELLATION,
                "fromMajor": from_major,
                "toMajor": to_major,
                "maxAlumni": limit,
            },
        )
    except Exception:
        pass  # Caching is an optimization; never fail the request over it.

    # The freshly computed response, not the stored copy: this one was not
    # served from cache, and `meta.cached` should say so.
    return response
