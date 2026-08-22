"""GET /api/students/me/insights — corpus intelligence personalized to the caller.

Analyzes the school's alumni corpus through the lens of the student's starting
position: which transitions are common from their major, where alumni who
started like them ended up, which of their courses signal which outcomes, and
when people typically changed direction.

Same auth and scoping as every other student route: the subject is the token
holder, the corpus is their school's, and the endpoint takes no student id.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.auth import current_student
from app.db import get_session
from app.matching import StudentProfile, build_corpus
from app.matching.insights import build_insights
from app.models import Student
from app.schemas.insights import (
    CourseSignalOut,
    InsightsResponse,
    OutcomeBreakdownOut,
    TransitionPatternOut,
)

router = APIRouter(prefix="/api/students", tags=["insights"])


@router.get("/me/insights", response_model=InsightsResponse, response_model_by_alias=True)
async def get_insights(
    student: Student = Depends(current_student),
    session: AsyncSession = Depends(get_session),
) -> InsightsResponse:
    alumni = await repository.list_alumni(session, school_id=student.school_id)
    profile = StudentProfile.from_model(student)
    corpus = build_corpus(alumni, school_id=student.school_id)

    insights = await asyncio.to_thread(build_insights, profile, corpus)

    return InsightsResponse(
        common_transitions=[
            TransitionPatternOut(
                from_major=t.from_major,
                to_major=t.to_major,
                count=t.count,
                typical_semester=t.typical_semester,
            )
            for t in insights.common_transitions
        ],
        outcome_distribution=[
            OutcomeBreakdownOut(
                career_area=o.career_area,
                count=o.count,
                percent=o.percent,
            )
            for o in insights.outcome_distribution
        ],
        course_signals=[
            CourseSignalOut(
                course_code=s.course_code,
                top_outcome=s.top_outcome,
                alumni_count=s.alumni_count,
            )
            for s in insights.course_signals
        ],
        pivot_timing=insights.pivot_timing,
        cohort_size=insights.cohort_size,
    )
