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


class PivotPointOut(CamelModel):
    """A term where this alumnus changed direction — the diamond on the timeline."""

    semester: str = Field(description='e.g. "Junior Fall"')
    from_major: str
    to_major: str


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


class MatchReasonOut(CamelModel):
    """Why this alumnus matched, in words, derived from the score components.

    Describes only what the student and alumnus share — never the alumnus's
    career outcome, which is `provenance='synthetic'` on the placeholder dataset
    and must not be asserted as fact inside a rationale.
    """

    summary: str = Field(description="One line, ready to render")
    factors: list[str] = Field(description="The same clauses unjoined, for chips")


class AlumnusOut(CamelModel):
    """One node on the map, nested under the cluster it belongs to.

    Note what is *not* here: `coursesBySemester`. A transcript is ~50 rows and
    this ships up to `maxAlumni` times per response; the detail panel fetches
    one alumnus at a time from `/api/alumni/{id}/timeline`, which is cached per
    (student, alumnus) and resolves courses relative to the caller.
    """

    id: str
    similarity_score: float = Field(
        ge=0.0, le=100.0, description="Match percentage. Drives node size and the match label"
    )
    graduation_year: int
    cluster: str = Field(description="The cluster's label, for node colouring")
    # Just the sentence on nodes: the structured form is on the detail payload,
    # and this one ships up to `maxAlumni` times per response.
    match_reason: str | None = Field(
        default=None, description="One-line rationale for the node tooltip"
    )
    majors: list[str]
    minors: list[str] = Field(default_factory=list)
    # The full picture — multiple majors, minors, and provenance. `majors` and
    # `minors` above are the flattened names, for clients that want only those.
    programs: list[ProgramOut] = Field(default_factory=list)
    career_outcome: OutcomeOut
    interests: list[str] = Field(default_factory=list)
    pivot_points: list[PivotPointOut] = Field(default_factory=list)


class ClusterOut(CamelModel):
    """A career-outcome cluster and the alumni in it.

    Alumni are nested rather than listed flat with a `clusterId`, because the
    grouping *is* the layout: the frontend draws one constellation per cluster.

    Still no geometry. `x`/`y` belong to the force layout, and the frontend can
    derive them from this grouping — shipping coordinates would freeze its
    layout model in the API.
    """

    id: str
    label: str = Field(description='Human-readable career area, e.g. "Health Policy"')
    similarity: float = Field(ge=0.0, le=1.0, description="Drives cluster radius on the frontend")
    top_majors: list[str] = Field(description="For the hover tooltip")
    alumni: list[AlumnusOut]


class ClusterEdgeOut(CamelModel):
    source: str
    target: str
    weight: float = Field(ge=0.0, le=1.0, description="Jaccard over the clusters' major sets")


class ConstellationResponse(CamelModel):
    student: StudentContext
    clusters: list[ClusterOut]
    cluster_edges: list[ClusterEdgeOut]
    total_alumni: int = Field(description="Alumni in the response, across all clusters")
    summary: str = Field(description="One line for the header, e.g. '50 alumni across 6 clusters'")
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
    status: str = Field(description="'kept' | 'new' | 'dropped'")


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
    # None when nothing specific matched — an alumnus can rank on neutral
    # defaults alone, and inventing a reason for those would be a fabrication.
    match_reason: MatchReasonOut | None = None


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
