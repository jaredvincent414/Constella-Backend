"""Response schemas.

These mirror the `ConstellationResponse` and `AcademicTimelineProps` contracts
in the frontend spec exactly. Field names are camelCase on the wire so the
frontend's TypeScript types need no translation layer.

Note what is *absent*: geometry. The backend sends similarity and cluster
assignment; radius, angle, and coordinates are the frontend's concern. Shipping
coordinates would freeze the frontend's ability to change its layout model.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


# --------------------------------------------------------------------------
# GET /api/constellation
# --------------------------------------------------------------------------


class StudentContext(CamelModel):
    year: str
    interests: list[str]
    courses: list[str]


class ClusterOut(CamelModel):
    id: str
    label: str = Field(description='Human-readable career area, e.g. "Health Policy"')
    similarity: float = Field(ge=0.0, le=1.0, description="Drives cluster radius on the frontend")
    top_majors: list[str] = Field(description="For the hover tooltip")
    member_count: int


class OutcomeOut(CamelModel):
    title: str
    org: str
    # Present once employment data is seeded/loaded. `provenance='synthetic'`
    # marks placeholder employment the UI must not present as real.
    industry: str | None = None
    occupation: str | None = None
    region: str | None = None
    provenance: str | None = None


class ProgramOut(CamelModel):
    """A major or minor with its provenance. `provenance='derived'` marks a
    program we inferred (e.g. a de facto minor) — the UI must label it inferred,
    never as formally declared."""

    code: str
    role: str = Field(description="'major' | 'minor'")
    provenance: str = Field(description="'reported' | 'derived'")


class AlumnusOut(CamelModel):
    id: str
    cluster_id: str
    similarity: float = Field(ge=0.0, le=1.0, description="Drives node size on the frontend")
    class_year: int
    # Graduation major names, unchanged. `programs` carries the full picture —
    # multiple majors, minors, and provenance — for clients that want it.
    majors: list[str]
    programs: list[ProgramOut] = Field(default_factory=list)
    outcome: OutcomeOut


class ClusterEdgeOut(CamelModel):
    source: str
    target: str
    weight: float = Field(ge=0.0, le=1.0, description="Jaccard over the clusters' major sets")


class ConstellationResponse(CamelModel):
    student: StudentContext
    clusters: list[ClusterOut]
    alumni: list[AlumnusOut]
    cluster_edges: list[ClusterEdgeOut]
    meta: ConstellationMeta


class ConstellationMeta(CamelModel):
    """Diagnostics. Not consumed by the render path — useful for debugging a
    thin or empty constellation, which the frontend spec flags as an open
    question."""

    cached: bool
    generated_at: str
    total_candidates: int = Field(description="Alumni scored before the top-N cut")
    returned: int
    edges_before_pruning: int


# --------------------------------------------------------------------------
# GET /api/alumni/{id}/timeline  — lazily fetched on node click
# --------------------------------------------------------------------------


class TimelineCourse(CamelModel):
    name: str
    status: str = Field(description="'kept' | 'added' | 'dropped'")


class TimelineSemester(CamelModel):
    label: str = Field(description='e.g. "Sophomore Spring"')
    is_pivot: bool
    courses: list[TimelineCourse]
    milestone: str | None = None


class ScoreBreakdown(CamelModel):
    """Why this alumnus matched. Every component is in 0..1 before weighting,
    so the frontend can render the contributions directly."""

    course_overlap: float
    pivot_year_alignment: float
    major_match: float
    interest_overlap: float
    total: float
    shared_courses: list[str]


class AlumnusDetail(CamelModel):
    id: str
    class_year: int
    similarity: float
    majors: list[str]
    programs: list[ProgramOut] = Field(default_factory=list)
    outcome: OutcomeOut
    interests: list[str]
    semesters: list[TimelineSemester]
    score_breakdown: ScoreBreakdown | None = None


# --------------------------------------------------------------------------
# POST /api/simulate  — the What If Simulator
# --------------------------------------------------------------------------


class SimulationRequest(CamelModel):
    # No studentId: the subject is the authenticated caller. Accepting one here
    # would let anyone simulate — and read timelines — as another student.
    from_major: str | None = Field(
        default=None, description="Defaults to the student's declared major"
    )
    to_major: str = Field(description="Target major or career area")
    top_n: int | None = Field(default=None, ge=1, le=25)


class SimulationMatch(CamelModel):
    alumnus: AlumnusDetail
    pivot_semester: str | None
    pivot_from: str | None
    pivot_to: str | None
    # How the major set changed at the pivot: 'added' (kept the first major and
    # gained another), 'dropped', or 'switched' (replaced). Lets the What-If UI
    # distinguish "added a minor" from "switched majors".
    pivot_type: str | None = None


class SimulationResponse(CamelModel):
    student: StudentContext
    from_major: str | None
    to_major: str
    matches: list[SimulationMatch]
    total_candidates: int


ConstellationResponse.model_rebuild()
