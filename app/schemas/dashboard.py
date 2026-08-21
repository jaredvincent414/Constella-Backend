"""Schemas for the Dashboard landing page.

Every number here is a projection of something the backend already produces —
the caller's constellation — plus a live count of their saved paths. Nothing is
derived a second way, so the "highest match" card and the brightest node on the
map cannot disagree.
"""

from __future__ import annotations

from pydantic import Field

from app.schemas.constellation import AlumnusOut, CamelModel


class DashboardStats(CamelModel):
    """The four counters across the top of the page."""

    alumni_matches: int = Field(description="Alumni in the caller's current constellation")
    clusters: int = Field(
        description=(
            "Career clusters in that constellation. The page labels this card "
            "'Clusters Explored', which means clusters the student has visited — "
            "that needs an activity log, and this service keeps none"
        )
    )
    # None rather than 0 on an empty constellation: 0 renders as "your best
    # match is 0%", when what happened is that nothing matched at all.
    highest_match: float | None = Field(
        default=None, ge=0.0, le=100.0, description="Top similarity score, 0..100"
    )
    saved_paths: int = Field(description="Bookmarked paths, counted live")


class DashboardMeta(CamelModel):
    cached: bool = Field(description="Whether the constellation behind these numbers was a hit")
    generated_at: str | None = Field(
        default=None, description="When that constellation was built"
    )


class DashboardResponse(CamelModel):
    stats: DashboardStats
    # The same `AlumnusOut` the constellation nests under each cluster, not a
    # trimmed copy shaped for a card. A card and the node it links to are one
    # alumnus, and a second vocabulary for one idea is how the two drift — a
    # trimmed card is also exactly where the outcome's `provenance` flag gets
    # dropped, and these outcomes are synthetic.
    top_matches: list[AlumnusOut]
    meta: DashboardMeta
