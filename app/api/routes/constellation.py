"""GET /api/constellation — the primary payload behind the constellation map."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response

from app import cache, repository
from app.api.responses import cached_json
from app.auth import Principal, current_principal
from app.config import settings
from app.db import SessionLocal
from app.jobs.recompute import ExploreQuery
from app.matching import StudentProfile, build_constellation_for_query, parse_interests
from app.models import ActivityKind
from app.schemas import ConstellationResponse

router = APIRouter(prefix="/api", tags=["constellation"])


@router.get("/constellation", response_model=ConstellationResponse, response_model_by_alias=True)
async def get_constellation(
    request: Request,
    background: BackgroundTasks,
    to_major: str | None = Query(default=None, alias="toMajor"),
    from_major: str | None = Query(default=None, alias="fromMajor"),
    interests: str | None = Query(
        default=None, description="Comma-separated, e.g. Biology,Psychology. Matches on any."
    ),
    career_area: str | None = Query(default=None, alias="careerArea"),
    major: str | None = Query(
        default=None,
        description="Matches either end of the path — started in it, or graduated in it",
    ),
    max_alumni: int | None = Query(default=None, alias="maxAlumni", ge=1, le=1000),
    refresh: bool = Query(default=False, description="Bypass the cache and recompute"),
    principal: Principal = Depends(current_principal),
) -> ConstellationResponse | Response:
    """Serve the authenticated student's constellation, cached.

    On a hit this returns Redis's copy untouched. On a miss it computes inline
    and backfills the cache, so a cold or unavailable Redis costs latency rather
    than availability.

    There is no `studentId` parameter — the subject is the token holder, so the
    cache key is derived from an identity the caller cannot choose.

    No session dependency: on a hit this route needs Postgres for nothing but
    identity, and identity comes from `current_principal`, which is cached. The
    miss path opens its own session. A dependency would be resolved eagerly and
    check out a connection for every hit.
    """
    student_id = principal.id
    query = ExploreQuery.build(
        max_alumni=max_alumni or settings.constellation_max_alumni,
        from_major=from_major,
        to_major=to_major,
        interests=parse_interests(interests),
        career_area=career_area,
        major=major,
    )
    # Every facet is in the key. One left out would mean a filtered result
    # written over the unfiltered entry, then served to the next request that
    # asked for everything.
    key = cache.constellation_key(student_id, query.cache_hash())

    # A deliberate exploration is worth remembering; a page load is not. Queued
    # as a background task so it runs *after* the response is written — a cache
    # hit still returns without waiting on Postgres, and a failed write costs a
    # log line rather than the request.
    if not query.is_broad:
        background.add_task(_log_exploration, student_id, query)

    if not refresh:
        try:
            cached_raw = await cache.get_raw(key)
        except Exception:
            cached_raw = None  # Redis down — fall through and compute.
        if cached_raw is not None:
            return cached_json(cached_raw, request)

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
                return cached_json(waited, request)

    try:
        return await _compute_and_cache(principal, key, query)
    finally:
        if not refresh:
            try:
                await cache.release_compute_lock(key)
            except Exception:
                pass


async def _compute_and_cache(
    principal: Principal, key: str, query: ExploreQuery
) -> ConstellationResponse:
    async with SessionLocal() as session:
        student = await repository.get_student(session, principal.id)
        if student is None:
            # A cached principal outlives its student by up to the auth TTL. On
            # a hit that costs nothing — the payload is theirs either way — but
            # there is nothing to recompute from, and a 401 is the honest answer
            # rather than a 500 on a None profile.
            raise HTTPException(status_code=401, detail="Invalid token")

        # Only this student's own school. The corpus is the tenant boundary: an
        # alumnus from another school can never reach the response, scored or not.
        alumni = await repository.list_alumni(session, school_id=principal.school_id)
        profile = StudentProfile.from_model(student)

    # Scoring is synchronous, CPU-bound, and proportional to the corpus size —
    # run inline it would block the event loop for every other request this
    # worker is serving. The thread never touches the session, and the corpus is
    # fully eager-loaded, so no lazy load can fire from off-loop.
    response = await asyncio.to_thread(
        build_constellation_for_query,
        profile,
        alumni,
        query.from_major,
        query.to_major,
        query.max_alumni,
        list(query.interests),
        query.career_area,
        query.major,
    )

    try:
        payload = response.model_dump(by_alias=True)
        await cache.set_raw(key, cache.serialize_cached(payload))
        await cache.track_student_key(principal.id, key, query.as_params())
    except Exception:
        pass  # Caching is an optimization; never fail the request over it.

    # The freshly computed response, not the stored copy: this one was not
    # served from cache, and `meta.cached` should say so.
    return response


def exploration_label(query: ExploreQuery) -> str:
    """What the student was looking for, in the words they chose.

    Most specific facet wins rather than concatenating all of them: "Explored
    Health Policy · Biology · Economics → Public Health" is a query string, not
    a memory. One clause is what makes a feed skimmable.
    """
    if query.to_major:
        return f"Explored a move to {query.to_major}"[:200]
    if query.career_area:
        return f"Explored the {query.career_area} cluster"[:200]
    if query.major:
        return f"Explored {query.major} paths"[:200]
    if query.interests:
        return f"Explored interests: {', '.join(query.interests)}"[:200]
    return "Explored the constellation"


async def _log_exploration(student_id: str, query: ExploreQuery) -> None:
    """Runs after the response. Opens its own session because this route
    deliberately has no session dependency — one would be resolved eagerly and
    check out a connection on every cache hit."""
    async with SessionLocal() as session:
        await repository.record_activity(
            session, student_id, ActivityKind.explored, exploration_label(query)
        )
