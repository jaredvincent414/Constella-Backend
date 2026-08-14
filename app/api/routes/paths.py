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

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, repository
from app.auth import current_student
from app.db import get_session
from app.matching import StudentProfile, build_detail, score_corpus
from app.matching.combine import combine_paths
from app.models import Student
from app.schemas import (
    AlumnusDetail,
    CombineRequest,
    CombineResponse,
    SavedPathOut,
    SavePathRequest,
)

router = APIRouter(prefix="/api/paths", tags=["paths"])


async def _detail_for(
    session: AsyncSession, alumnus_id: str, profile, school_id: str | None
) -> AlumnusDetail:
    alumnus = await repository.get_alumnus(session, alumnus_id, school_id=school_id)
    if alumnus is None:
        raise HTTPException(status_code=404, detail=f"Alumnus {alumnus_id!r} not found")
    scored = score_corpus(profile, [alumnus])[0] if profile else None
    return build_detail(scored, alumnus, profile)


@router.get("", response_model=list[SavedPathOut], response_model_by_alias=True)
async def list_paths(
    student: Student = Depends(current_student),
    session: AsyncSession = Depends(get_session),
) -> list[SavedPathOut]:
    profile = StudentProfile.from_model(student)
    saved = await repository.list_saved_paths(session, student.id)
    return [
        SavedPathOut(
            id=path.id,
            saved_at=path.saved_at.isoformat(),
            notes=path.notes,
            alumnus=await _detail_for(session, path.alumnus_id, profile, student.school_id),
        )
        for path in saved
    ]


@router.post("", response_model=SavedPathOut, response_model_by_alias=True, status_code=201)
async def create_path(
    request: SavePathRequest,
    student: Student = Depends(current_student),
    session: AsyncSession = Depends(get_session),
) -> SavedPathOut:
    # School-scoped before the write: bookmarking is otherwise a way to smuggle a
    # foreign alumnus id into a row this student is allowed to read back.
    if await repository.get_alumnus(session, request.alumnus_id, student.school_id) is None:
        raise HTTPException(status_code=404, detail=f"Alumnus {request.alumnus_id!r} not found")

    path = await repository.save_path(session, student.id, request.alumnus_id, request.notes)
    profile = StudentProfile.from_model(student)
    return SavedPathOut(
        id=path.id,
        saved_at=path.saved_at.isoformat(),
        notes=path.notes,
        alumnus=await _detail_for(session, path.alumnus_id, profile, student.school_id),
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
) -> CombineResponse:
    saved = await repository.get_saved_paths_by_ids(session, student.id, request.path_ids)
    if len(saved) < 2:
        raise HTTPException(
            status_code=400,
            detail="Combine needs at least 2 of the student's own saved paths.",
        )

    alumnus_ids = [p.alumnus_id for p in saved]
    key = cache.combined_path_key(student.id, alumnus_ids)
    try:
        cached_payload = await cache.get_json(key)
    except Exception:
        cached_payload = None
    if cached_payload is not None:
        return CombineResponse.model_validate(cached_payload)

    profile = StudentProfile.from_model(student)
    alumni = []
    for alumnus_id in alumnus_ids:
        alumnus = await repository.get_alumnus(session, alumnus_id, school_id=student.school_id)
        if alumnus is not None:
            alumni.append(alumnus)

    response = combine_paths(profile, alumni)

    try:
        await cache.set_json(key, response.model_dump(by_alias=True))
        await cache.track_student_key(student.id, key)
    except Exception:
        pass  # caching is an optimization; never fail the request over it

    return response
