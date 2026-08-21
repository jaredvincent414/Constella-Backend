"""GET /api/students/me/dashboard — the counters on the Dashboard landing page.

Four stats and a short list of top matches, all of them a projection of the
caller's constellation plus a live count of their saved paths. The dashboard
derives nothing on its own: every number here came out of the same pipeline the
map did, so the page and the map cannot tell the student two different stories.

One card the design asks for is answered narrowly on purpose. "Clusters
Explored" means clusters the student has *visited*, which needs an activity log
this service does not keep and should not grow as a side effect of a stats
endpoint. What can be answered honestly is how many clusters are in the
constellation they are exploring, so that is what `stats.clusters` counts and
what its description says on the wire.

No `studentId` parameter: the subject is the token holder, and the constellation
this reads is keyed by an identity the caller cannot choose.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, repository
from app.auth import Principal, current_principal
from app.config import settings
from app.db import get_session
from app.jobs.recompute import ExploreQuery
from app.matching import StudentProfile, build_constellation_for_query
from app.schemas import AlumnusOut, DashboardMeta, DashboardResponse, DashboardStats

router = APIRouter(prefix="/api/students", tags=["dashboard"])

DEFAULT_TOP_MATCHES = 4


def broad_query() -> ExploreQuery:
    """The unfiltered explore query — the constellation this page summarizes.

    Built through `ExploreQuery` rather than assembled here so the dashboard
    reads the entry `/api/constellation` writes and the nightly job warms,
    instead of a second entry holding the same payload under a different hash.
    """
    return ExploreQuery.build(max_alumni=settings.constellation_max_alumni)


@router.get("/me/dashboard", response_model=DashboardResponse, response_model_by_alias=True)
async def get_dashboard(
    top_matches: int = Query(
        default=DEFAULT_TOP_MATCHES,
        alias="topMatches",
        ge=1,
        le=20,
        description="How many top-ranked alumni to return",
    ),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> DashboardResponse:
    """Serve the caller's dashboard, reusing their cached constellation.

    `topMatches` is deliberately *not* part of the cache key: it slices a list
    the entry already holds in full. Folding it in would fork one payload into
    twenty identical entries — and worse, a request for four would write an entry
    the constellation route would then serve as the whole map.

    This takes a session dependency where the constellation route pointedly does
    not: the saved-path count is read live on every request, hit or miss, so
    there is no path here that keeps off Postgres and nothing for a lazily opened
    session to save.
    """
    query = broad_query()
    key = cache.constellation_key(principal.id, query.cache_hash())

    try:
        raw = await cache.get_raw(key)
    except Exception:
        raw = None  # Redis down — fall through and compute.

    payload = _decode(raw) if raw is not None else None
    cached = payload is not None
    if payload is None:
        payload = await _constellation_payload(session, principal, query)

    # Counted, not stored: a saved path changes this number without touching the
    # constellation, so caching it beside one would show a student a stale count
    # of their own bookmarks.
    saved_paths = len(await repository.list_saved_paths(session, principal.id))

    response = build_dashboard(
        payload, saved_paths=saved_paths, limit=top_matches, cached=cached
    )
    if not cached:
        await _backfill(principal.id, key, payload, query)
    return response


def build_dashboard(
    payload: dict, saved_paths: int, limit: int, cached: bool
) -> DashboardResponse:
    """Project a constellation payload onto the dashboard's stats and top N.

    Takes the wire dict rather than a `ConstellationResponse`, because the cache
    hit already has one and revalidating up to `maxAlumni` alumni to read four of
    them is work spent on records nobody looks at.
    """
    clusters = payload.get("clusters") or []
    # The payload groups alumni by cluster because the grouping is the layout;
    # the dashboard list is flat. Ties break on id, the same way the scorer
    # breaks them, so the same four cards come back for the same constellation.
    ranked = sorted(
        (alumnus for cluster in clusters for alumnus in (cluster.get("alumni") or [])),
        key=lambda a: (-(a.get("similarityScore") or 0.0), a.get("id") or ""),
    )
    meta = payload.get("meta") or {}
    return DashboardResponse(
        stats=DashboardStats(
            # `totalAlumni` rather than a recount: the pipeline decided which
            # alumni survived clustering, and it already published the number.
            alumni_matches=payload.get("totalAlumni", len(ranked)),
            clusters=len(clusters),
            highest_match=ranked[0].get("similarityScore") if ranked else None,
            saved_paths=saved_paths,
        ),
        top_matches=[AlumnusOut.model_validate(alumnus) for alumnus in ranked[:limit]],
        meta=DashboardMeta(cached=cached, generated_at=meta.get("generatedAt")),
    )


def _decode(raw: bytes) -> dict | None:
    """The cached constellation, parsed.

    The constellation route hands these bytes to the socket untouched; this page
    is a different projection of them, so it is the one that pays the parse. An
    entry that doesn't decode behaves as a miss rather than a 500 — it is one
    recompute away from correct either way.
    """
    try:
        return json.loads(cache.decompress(raw))
    except Exception:
        return None


async def _constellation_payload(
    session: AsyncSession, principal: Principal, query: ExploreQuery
) -> dict:
    student = await repository.get_student(session, principal.id)
    if student is None:
        # A cached principal outlives its student by up to the auth TTL. There is
        # nothing left to score against, and 401 is the honest answer rather than
        # a 500 on a None profile.
        raise HTTPException(status_code=401, detail="Invalid token")

    # Only this student's own school. The corpus is the tenant boundary: an
    # alumnus from another school can never reach a stat, let alone a card.
    alumni = await repository.list_alumni(session, school_id=principal.school_id)
    profile = StudentProfile.from_model(student)

    # Same reason as the constellation route: scoring is CPU-bound and
    # proportional to the corpus, and it would otherwise block the event loop for
    # every other request this worker is serving.
    response = await asyncio.to_thread(
        build_constellation_for_query,
        profile,
        alumni,
        None,
        None,
        query.max_alumni,
    )
    return response.model_dump(by_alias=True)


async def _backfill(student_id: str, key: str, payload: dict, query: ExploreQuery) -> None:
    """Store what this request computed under the constellation route's own key.

    The two pages share one entry, so a student who lands on the dashboard first
    has warmed Explore by the time they click through.
    """
    try:
        await cache.set_raw(key, cache.serialize_cached(payload))
        await cache.track_student_key(student_id, key, query.as_params())
    except Exception:
        pass  # Caching is an optimization; never fail the request over it.
