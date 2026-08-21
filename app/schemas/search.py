"""Schemas for GET /api/search — the topbar typeahead.

One flat, uniformly-shaped list rather than three keyed arrays: a dropdown
renders rows, and a shape that varies per kind forces the frontend to branch on
`type` before it can draw anything. `detail`, `count`, and `provenance` are
optional precisely so the row stays one type.
"""

from __future__ import annotations

from pydantic import Field

from app.schemas.constellation import CamelModel


class SearchResultOut(CamelModel):
    """One row of the dropdown."""

    type: str = Field(description="'major' | 'cluster' | 'alumnus'")
    id: str = Field(
        description=(
            "Alumnus id, or the cluster/major slug — the same id the constellation "
            "uses, so selecting a row can address what it names"
        )
    )
    label: str = Field(description='What to render, e.g. "Biochemistry" or "Class of 2022"')
    detail: str | None = Field(default=None, description="Secondary line, when there is one")
    count: int | None = Field(
        default=None, description="Alumni behind a major or cluster; null for an alumnus"
    )
    provenance: str | None = Field(
        default=None,
        description=(
            "'synthetic' when the label came from placeholder employment data — the "
            "UI must not present it as reported fact"
        ),
    )


class SearchResponse(CamelModel):
    query: str = Field(description="The normalized query the results were matched against")
    results: list[SearchResultOut]
    total: int = Field(
        description=(
            "How many results are in this response, not how many exist. A typeahead "
            "has no use for a corpus-wide count, and it would cost three more aggregates"
        )
    )
