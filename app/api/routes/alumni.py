"""GET /api/alumni/{id}/timeline — the lazily-fetched detail panel payload.

Kept off the constellation response on purpose: semester-by-semester course data
for 200 alumni would multiply the initial payload for data the student may never
open. The frontend fetches this on node click and shows a skeleton meanwhile.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.db import get_session
from app.matching import StudentProfile, build_detail, score_corpus
from app.schemas import AlumnusDetail

router = APIRouter(prefix="/api/alumni", tags=["alumni"])


@router.get("/{alumnus_id}/timeline", response_model=AlumnusDetail, response_model_by_alias=True)
async def get_timeline(
    alumnus_id: str,
    student_id: str | None = Query(default=None, alias="studentId"),
    session: AsyncSession = Depends(get_session),
) -> AlumnusDetail:
    """Full academic timeline for one alumnus.

    Passing `studentId` resolves each course as kept/added relative to that
    student and includes the score breakdown. Without it the timeline is still
    served, just without the comparison.
    """
    alumnus = await repository.get_alumnus(session, alumnus_id)
    if alumnus is None:
        raise HTTPException(status_code=404, detail=f"Alumnus {alumnus_id!r} not found")

    profile = None
    scored = None
    if student_id:
        student = await repository.get_student(session, student_id)
        if student is None:
            raise HTTPException(status_code=404, detail=f"Student {student_id!r} not found")
        profile = StudentProfile.from_model(student)
        # Scoring a single-element corpus reuses the exact same code path as the
        # constellation, so the panel can never disagree with the node it opened.
        scored = score_corpus(profile, [alumnus])[0]

    return build_detail(scored, alumnus, profile)
