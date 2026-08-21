"""POST /api/simulate — the What If Simulator.

Same scoring engine as the constellation, narrowed to alumni whose pivot
actually answers the student's question and cut to the top 5.

The response is the Transition page's shape: a header of aggregates computed
over every matching candidate, then ranked cards. Card assembly lives in
`app.matching.transitions` so it can be tested without a request.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.auth import current_student
from app.config import settings
from app.db import get_session
from app.matching import StudentProfile, filter_by_pivot_query, score_corpus
from app.matching.transitions import build_card, peak_timing, top_outcome
from app.models import Student
from app.schemas import SimulationRequest, SimulationResponse

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

    top_n = request.top_n or settings.simulator_top_n
    # Cut inside the scorer: a What If keeps a handful of matches out of the
    # whole filtered corpus, and the rest never need a breakdown built.
    scored = score_corpus(profile, candidates, top_n=top_n)

    label, count = top_outcome(candidates)
    return SimulationResponse(
        from_major=from_major,
        to_major=request.to_major,
        # Over every candidate, not the cards: this is a fact about the corpus.
        total_transitions=len(candidates),
        peak_timing=peak_timing(candidates),
        top_outcome=label,
        top_outcome_count=count,
        cards=[
            build_card(item, profile, is_top_match=(index == 0))
            for index, item in enumerate(scored)
        ],
    )
