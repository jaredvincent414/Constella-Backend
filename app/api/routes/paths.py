"""Create Path — saved alumni paths and path combining.

    GET    /api/paths?studentId=      saved paths (with full timeline)
    POST   /api/paths                 bookmark an alumnus
    DELETE /api/paths/{id}?studentId=  remove a bookmark
    POST   /api/paths/combine         merge 2+ saved paths into one plan

The combine result is cached in Redis keyed by the student + the sorted set of
alumni combined, and tracked under the student's index so a profile change
invalidates it alongside the constellation.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, repository
from app.db import get_session
from app.matching import StudentProfile, build_detail, score_corpus
from app.matching.combine import combine_paths
from app.schemas import (
    AlumnusDetail,
    CombineRequest,
    CombineResponse,
    SavedPathOut,
    SavePathRequest,
)

router = APIRouter(prefix="/api/paths", tags=["paths"])


async def _detail_for(session: AsyncSession, alumnus_id: str, profile) -> AlumnusDetail:
    alumnus = await repository.get_alumnus(session, alumnus_id)
    if alumnus is None:
        raise HTTPException(status_code=404, detail=f"Alumnus {alumnus_id!r} not found")
    scored = score_corpus(profile, [alumnus])[0] if profile else None
    return build_detail(scored, alumnus, profile)


@router.get("", response_model=list[SavedPathOut], response_model_by_alias=True)
async def list_paths(
    student_id: str = Query(alias="studentId"),
    session: AsyncSession = Depends(get_session),
) -> list[SavedPathOut]:
    student = await repository.get_student(session, student_id)
    if student is None:
        raise HTTPException(status_code=404, detail=f"Student {student_id!r} not found")
    profile = StudentProfile.from_model(student)

    saved = await repository.list_saved_paths(session, student_id)
    return [
        SavedPathOut(
            id=path.id,
            saved_at=path.saved_at.isoformat(),
            notes=path.notes,
            alumnus=await _detail_for(session, path.alumnus_id, profile),
        )
        for path in saved
    ]


@router.post("", response_model=SavedPathOut, response_model_by_alias=True, status_code=201)
async def create_path(
    request: SavePathRequest,
    session: AsyncSession = Depends(get_session),
) -> SavedPathOut:
    student = await repository.get_student(session, request.student_id)
    if student is None:
        raise HTTPException(status_code=404, detail=f"Student {request.student_id!r} not found")
    if await repository.get_alumnus(session, request.alumnus_id) is None:
        raise HTTPException(status_code=404, detail=f"Alumnus {request.alumnus_id!r} not found")

    path = await repository.save_path(
        session, request.student_id, request.alumnus_id, request.notes
    )
    profile = StudentProfile.from_model(student)
    return SavedPathOut(
        id=path.id,
        saved_at=path.saved_at.isoformat(),
        notes=path.notes,
        alumnus=await _detail_for(session, path.alumnus_id, profile),
    )


@router.delete("/{path_id}", status_code=204)
async def delete_path(
    path_id: int,
    student_id: str = Query(alias="studentId"),
    session: AsyncSession = Depends(get_session),
) -> None:
    removed = await repository.delete_saved_path(session, student_id, path_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Saved path {path_id} not found")


@router.post("/combine", response_model=CombineResponse, response_model_by_alias=True)
async def combine(
    request: CombineRequest,
    session: AsyncSession = Depends(get_session),
) -> CombineResponse:
    student = await repository.get_student(session, request.student_id)
    if student is None:
        raise HTTPException(status_code=404, detail=f"Student {request.student_id!r} not found")

    saved = await repository.get_saved_paths_by_ids(session, request.student_id, request.path_ids)
    if len(saved) < 2:
        raise HTTPException(
            status_code=400,
            detail="Combine needs at least 2 of the student's own saved paths.",
        )

    alumnus_ids = [p.alumnus_id for p in saved]
    key = cache.combined_path_key(request.student_id, alumnus_ids)
    try:
        cached_payload = await cache.get_json(key)
    except Exception:
        cached_payload = None
    if cached_payload is not None:
        return CombineResponse.model_validate(cached_payload)

    profile = StudentProfile.from_model(student)
    alumni = []
    for alumnus_id in alumnus_ids:
        alumnus = await repository.get_alumnus(session, alumnus_id)
        if alumnus is not None:
            alumni.append(alumnus)

    response = combine_paths(profile, alumni)

    try:
        await cache.set_json(key, response.model_dump(by_alias=True))
        await cache.track_student_key(request.student_id, key)
    except Exception:
        pass  # caching is an optimization; never fail the request over it

    return response
