"""Schemas for corpus intelligence — patterns across the alumni corpus,
personalized to the student's starting position."""

from __future__ import annotations

from pydantic import Field

from app.schemas.constellation import CamelModel


class TransitionPatternOut(CamelModel):
    from_major: str
    to_major: str
    count: int = Field(description="Alumni who made this transition")
    typical_semester: str | None = Field(
        default=None, description='When most made the switch, e.g. "sophomore"'
    )


class OutcomeBreakdownOut(CamelModel):
    career_area: str
    count: int
    percent: float = Field(description="Percentage of cohort in this area")


class CourseSignalOut(CamelModel):
    course_code: str = Field(description="A course the student has taken")
    top_outcome: str = Field(description="Most common outcome for alumni who also took it")
    alumni_count: int = Field(description="Alumni who shared this course")


class InsightsResponse(CamelModel):
    common_transitions: list[TransitionPatternOut] = Field(
        description="Most common major changes among alumni who started like you"
    )
    outcome_distribution: list[OutcomeBreakdownOut] = Field(
        description="Where alumni with your starting major ended up"
    )
    course_signals: list[CourseSignalOut] = Field(
        description="Which of your courses correlate with specific outcomes"
    )
    pivot_timing: str | None = Field(
        default=None,
        description="When people with your major typically changed direction",
    )
    cohort_size: int = Field(description="Alumni who share your starting major")
