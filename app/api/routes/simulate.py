"""POST /api/simulate — the What If Simulator.

Same scoring engine as the constellation, narrowed to alumni whose pivot
actually answers the student's question and cut to the top 5.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.auth import current_student
from app.config import settings
from app.db import get_session
from app.matching import (
    StudentProfile,
    build_detail,
    detect_pivots,
    filter_by_pivot_query,
    score_corpus,
)
from app.models import Student, StudentYear, semester_label
from app.schemas import (
    SimulationMatch,
    SimulationRequest,
    SimulationResponse,
    StudentContext,
)

router = APIRouter(prefix="/api", tags=["simulator"])


@router.post("/simulate", response_model=SimulationResponse, response_model_by_alias=True)
async def simulate(
    request: SimulationRequest,
    student: Student = Depends(current_student),
    session: AsyncSession = Depends(get_session),
) -> SimulationResponse:
    """Run a What If for the authenticated student against their own school."""
    profile = StudentProfile.from_model(student)
    from_major = request.from_major or profile.declared_major
    # Override the profile so the 20% major-match component scores against the
    # hypothetical pivot rather than the student's saved intent. The major set is
    # replaced wholesale — this is a "what if I were starting from X" query.
    profile.declared_major = from_major
    profile.current_majors = {from_major} if from_major else set()
    profile.intended_direction = request.to_major

    alumni = await repository.list_alumni(session, school_id=student.school_id)
    candidates = filter_by_pivot_query(alumni, from_major, request.to_major)

    scored = score_corpus(profile, candidates)
    top_n = request.top_n or settings.simulator_top_n

    matches: list[SimulationMatch] = []
    for item in scored[:top_n]:
        pivot = item.alumnus.first_pivot
        # Set-diff pivot type (added/dropped/switched) from the program timeline;
        # matched to the stored pivot's term when there is one.
        changes = detect_pivots(item.alumnus)
        if pivot is not None:
            change = next(
                (c for c in changes if c.role == "major" and c.term == pivot.semester_index),
                next((c for c in changes if c.role == "major"), None),
            )
        else:
            change = next((c for c in changes if c.role == "major"), None)
        matches.append(
            SimulationMatch(
                alumnus=build_detail(item, item.alumnus, profile),
                pivot_semester=semester_label(pivot.semester_index) if pivot else None,
                pivot_from=pivot.from_major if pivot else None,
                pivot_to=pivot.to_major if pivot else None,
                pivot_type=change.kind if change else None,
            )
        )

    return SimulationResponse(
        student=StudentContext(
            year=StudentYear.from_index(profile.year_index).value,
            interests=profile.interests,
            courses=[profile.course_names[c] for c in profile.course_codes],
        ),
        from_major=from_major,
        to_major=request.to_major,
        matches=matches,
        total_candidates=len(candidates),
    )
