"""Schemas for the Create Path feature — saved paths and path combining.

camelCase on the wire (via `CamelModel`), matching the frontend contract in the
backend spec.
"""

from __future__ import annotations

from pydantic import Field

from app.schemas.constellation import AlumnusDetail, CamelModel

# --------------------------------------------------------------------------
# Saved paths
# --------------------------------------------------------------------------


class SavePathRequest(CamelModel):
    student_id: str
    alumnus_id: str
    notes: str | None = None


class SavedPathOut(CamelModel):
    id: int
    saved_at: str
    notes: str | None = None
    # Full alumnus detail incl. the semester timeline, resolved relative to the
    # student — the same payload the detail panel renders.
    alumnus: AlumnusDetail


# --------------------------------------------------------------------------
# Combine
# --------------------------------------------------------------------------


class CombineRequest(CamelModel):
    student_id: str
    path_ids: list[int] = Field(min_length=2, description="Saved-path ids to merge (2+)")


class CombinedPath(CamelModel):
    # Year-stage label ("Freshman".."Senior") -> the courses kept for that stage.
    semesters: dict[str, list[str]]
    outcome_fields: list[str] = Field(description="Distinct career areas the paths lead to")
    confidence: float = Field(ge=0.0, le=1.0, description="Shared / total-unique courses")


class SourcePath(CamelModel):
    alumnus_id: str
    major: str
    color_index: int = Field(description="Stable per-path color, also the Sankey path id")


class SankeyNode(CamelModel):
    id: str = Field(description='"{stage}-{label}", stage len(stages)=outcome row')
    stage: int
    label: str
    path_ids: list[int] = Field(description="color_index of each source path flowing through")


class SankeyLink(CamelModel):
    source: str
    target: str
    path_id: int = Field(description="color_index of the path this bezier belongs to")


class SankeyData(CamelModel):
    nodes: list[SankeyNode]
    links: list[SankeyLink]


class CombineResponse(CamelModel):
    combined_path: CombinedPath
    source_paths: list[SourcePath]
    shared_courses: list[str]
    sankey: SankeyData
