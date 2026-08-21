"""Create Path — saved alumni paths and path combining.

    GET    /api/paths            the caller's saved paths (with full timeline)
    POST   /api/paths            bookmark an alumnus
    DELETE /api/paths/{id}       remove a bookmark
    POST   /api/paths/combine    merge 2+ saved paths into one plan

Every route is scoped to the authenticated student — there is no `studentId`
parameter to point at someone else's bookmarks. Deleting a path that belongs to
another student 404s, since the lookup is filtered by owner before the id is
ever matched.

The combine result is cached in Redis keyed by the student + the sorted set of
alumni combined, and tracked under the student's index so a profile change
invalidates it alongside the constellation.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, repository
from app.auth import current_student
from app.db import get_session
from app.matching import StudentProfile, build_detail, score_corpus
from app.matching.combine import combine_paths
from app.models import Student
from app.schemas import (
    CombineRequest,
    CombineResponse,
    SavedPathOut,
    SavePathRequest,
)

router = APIRouter(prefix="/api/paths", tags=["paths"])


@router.get("", response_model=list[SavedPathOut], response_model_by_alias=True)
async def list_paths(
    student: Student = Depends(current_student),
    session: AsyncSession = Depends(get_session),
) -> list[SavedPathOut]:
    """The caller's saved paths, each with the full detail payload.

    Both the fetch and the scoring are done once for the whole set rather than
    per path. Scores are unaffected by the batching — every component is
    computed per alumnus against the student, so an alumnus scores the same
    alone as in company — but a per-path round trip made an ordinary list of
    bookmarks cost a query (plus five eager loads) apiece.
    """
    profile = StudentProfile.from_model(student)
    saved = await repository.list_saved_paths(session, student.id)

    alumni = await repository.list_alumni_by_ids(
        session, [path.alumnus_id for path in saved], school_id=student.school_id
    )
    by_id = {alumnus.id: alumnus for alumnus in alumni}
    scored_by_id = {item.alumnus.id: item for item in score_corpus(profile, alumni)}

    out: list[SavedPathOut] = []
    for path in saved:
        alumnus = by_id.get(path.alumnus_id)
        if alumnus is None:
            raise HTTPException(
                status_code=404, detail=f"Alumnus {path.alumnus_id!r} not found"
            )
        out.append(
            SavedPathOut(
                id=path.id,
                saved_at=path.saved_at.isoformat(),
                notes=path.notes,
                alumnus=build_detail(scored_by_id.get(alumnus.id), alumnus, profile),
            )
        )
    return out


@router.post("", response_model=SavedPathOut, response_model_by_alias=True, status_code=201)
async def create_path(
    request: SavePathRequest,
    student: Student = Depends(current_student),
    session: AsyncSession = Depends(get_session),
) -> SavedPathOut:
    # School-scoped before the write: bookmarking is otherwise a way to smuggle a
    # foreign alumnus id into a row this student is allowed to read back. The
    # record is kept rather than re-fetched for the response body.
    alumnus = await repository.get_alumnus(session, request.alumnus_id, student.school_id)
    if alumnus is None:
        raise HTTPException(status_code=404, detail=f"Alumnus {request.alumnus_id!r} not found")

    path = await repository.save_path(session, student.id, request.alumnus_id, request.notes)
    profile = StudentProfile.from_model(student)
    return SavedPathOut(
        id=path.id,
        saved_at=path.saved_at.isoformat(),
        notes=path.notes,
        alumnus=build_detail(score_corpus(profile, [alumnus])[0], alumnus, profile),
    )


@router.delete("/{path_id}", status_code=204)
async def delete_path(
    path_id: int,
    student: Student = Depends(current_student),
    session: AsyncSession = Depends(get_session),
) -> None:
    removed = await repository.delete_saved_path(session, student.id, path_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Saved path {path_id} not found")


@router.post("/combine", response_model=CombineResponse, response_model_by_alias=True)
async def combine(
    request: CombineRequest,
    student: Student = Depends(current_student),
    session: AsyncSession = Depends(get_session),
) -> CombineResponse | Response:
    saved = await repository.get_saved_paths_by_ids(session, student.id, request.path_ids)
    if len(saved) < 2:
        raise HTTPException(
            status_code=400,
            detail="Combine needs at least 2 of the student's own saved paths.",
        )

    alumnus_ids = [p.alumnus_id for p in saved]
    key = cache.combined_path_key(student.id, alumnus_ids)
    try:
        cached_raw = await cache.get_raw(key)
    except Exception:
        cached_raw = None
    if cached_raw is not None:
        return Response(content=cached_raw, media_type="application/json")

    profile = StudentProfile.from_model(student)
    # One query for the set. Ids outside the student's school simply don't come
    # back, which is the same silent drop the per-id loop did.
    alumni = await repository.list_alumni_by_ids(
        session, alumnus_ids, school_id=student.school_id
    )

    response = combine_paths(profile, alumni)

    try:
        await cache.set_raw(key, cache.serialize_cached(response.model_dump(by_alias=True)))
        await cache.track_student_key(student.id, key, {"kind": cache.KIND_COMBINE})
    except Exception:
        pass  # caching is an optimization; never fail the request over it

    return response
