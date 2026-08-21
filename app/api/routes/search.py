"""GET /api/search — the topbar typeahead, across majors, clusters, and alumni."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app import repository
from app.auth import Principal, current_principal
from app.db import SessionLocal
from app.schemas import SearchResponse, SearchResultOut
from app.search import MIN_QUERY_LENGTH, alumnus_hit, normalize_query, order_hits

router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search", response_model=SearchResponse, response_model_by_alias=True)
async def search(
    q: str = Query(max_length=64, description="What the student has typed so far"),
    limit: int = Query(
        default=5,
        ge=1,
        le=25,
        description="Max rows *per type*, so a dropdown always mixes all three",
    ),
    principal: Principal = Depends(current_principal),
) -> SearchResponse:
    """Mixed results for one query, scoped to the caller's school.

    `limit` is per type rather than overall. Course-code matches are numerous and
    score almost identically to each other, so a single global cut would let one
    common prefix fill the dropdown with alumni and hide the major the student
    was actually typing — the results have to stay mixed to be a search of
    "paths, majors…" rather than a search of whichever kind happens to win.

    There is no `schoolId` and no `studentId` parameter: the corpus searched is
    the token holder's, and a major, cluster, or course code from another school
    can never reach the response — its existence is what the tenant boundary is
    hiding.

    No session dependency. FastAPI resolves dependencies eagerly, so declaring
    one would check out a Postgres connection for every keystroke, including the
    short ones this answers without asking the database anything.
    """
    query = normalize_query(q)
    if len(query) < MIN_QUERY_LENGTH:
        return SearchResponse(query=query, results=[], total=0)

    async with SessionLocal() as session:
        scope = {"school_id": principal.school_id, "limit": limit}
        hits = [
            *await repository.search_majors(session, query, **scope),
            *await repository.search_career_clusters(session, query, **scope),
            *[
                alumnus_hit(alumnus, score, query)
                for alumnus, score in await repository.search_alumni_by_course(
                    session, query, **scope
                )
            ],
        ]

    results = [
        SearchResultOut(
            type=hit.kind,
            id=hit.id,
            label=hit.label,
            detail=hit.detail,
            count=hit.count,
            provenance=hit.provenance,
        )
        for hit in order_hits(hits)
    ]
    # Not cached. The key space is every prefix a student can type, each entry
    # used once, and the queries are index-backed and bounded by `limit` — a
    # cache here would evict the constellation entries that pay for themselves.
    return SearchResponse(query=query, results=results, total=len(results))
