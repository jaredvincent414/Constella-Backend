"""GET /api/alumni/{id}/timeline — the lazily-fetched detail panel payload.

Kept off the constellation response on purpose: semester-by-semester course data
for 200 alumni would multiply the initial payload for data the student may never
open. The frontend fetches this on node click and shows a skeleton meanwhile.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.auth import current_student
from app.db import get_session
from app.matching import StudentProfile, build_detail, score_corpus
from app.models import Student
from app.schemas import AlumnusDetail

router = APIRouter(prefix="/api/alumni", tags=["alumni"])


@router.get("/{alumnus_id}/timeline", response_model=AlumnusDetail, response_model_by_alias=True)
async def get_timeline(
    alumnus_id: str,
    student: Student = Depends(current_student),
    session: AsyncSession = Depends(get_session),
) -> AlumnusDetail:
    """Full academic timeline for one alumnus at the caller's school.

    Courses resolve as kept/added relative to the caller and the response carries
    their score breakdown — the comparison is the point of the panel, so the
    student is taken from the token rather than a query parameter.

    An id from another school 404s with the same body as an id that doesn't
    exist. Distinguishing the two would confirm which alumni a school has, which
    is exactly the enumeration this boundary is meant to prevent.
    """
    alumnus = await repository.get_alumnus(session, alumnus_id, school_id=student.school_id)
    if alumnus is None:
        raise HTTPException(status_code=404, detail=f"Alumnus {alumnus_id!r} not found")

    profile = StudentProfile.from_model(student)
    # Scoring a single-element corpus reuses the exact same code path as the
    # constellation, so the panel can never disagree with the node it opened.
    scored = score_corpus(profile, [alumnus])[0]

    return build_detail(scored, alumnus, profile)
