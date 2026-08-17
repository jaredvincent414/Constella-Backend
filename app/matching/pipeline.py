"""The precompute pipeline.

    score every alumnus  ->  rank  ->  cut to top-N  ->  cluster  ->  prune edges

This is the "background job" box in the spec's data flow diagram. It is written
as a pure function over already-loaded ORM objects so it can run either inside a
job or inline on a cache miss, without knowing which.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.config import settings
from app.matching.clustering import DEFAULT_MAX_CLUSTERS, build_cluster_edges, build_clusters
from app.matching.explain import explain_match
from app.matching.outcomes import build_outcome
from app.matching.programs import program_views
from app.matching.scoring import ScoredAlumnus, StudentProfile, score_corpus
from app.models import Alumnus
from app.schemas import (
    AlumnusOut,
    ClusterEdgeOut,
    ClusterOut,
    ConstellationMeta,
    ConstellationResponse,
    ProgramOut,
    StudentContext,
)


def build_constellation(
    profile: StudentProfile,
    alumni: list[Alumnus],
    max_alumni: int | None = None,
    max_clusters: int = DEFAULT_MAX_CLUSTERS,
    cached: bool = False,
) -> tuple[ConstellationResponse, list[ScoredAlumnus]]:
    """Produce the full constellation payload.

    Returns the response alongside the scored list, so a caller that also needs
    per-alumnus breakdowns doesn't have to score twice.
    """
    limit = max_alumni or settings.constellation_max_alumni

    scored = score_corpus(profile, alumni)
    total_candidates = len(scored)
    top = scored[:limit]

    clusters = build_clusters(top, max_clusters=max_clusters)
    edges, edges_before_pruning = build_cluster_edges(
        clusters,
        min_weight=settings.edge_min_weight,
        max_count=settings.edge_max_count,
    )

    # Clustering may drop low-similarity clusters past the layout ceiling, so
    # the alumni list is rebuilt from surviving cluster membership rather than
    # from `top` — otherwise the response would reference cluster ids that
    # aren't in `clusters`.
    alumni_out: list[AlumnusOut] = []
    for cluster in clusters:
        for member in cluster.members:
            record = member.alumnus
            reason = explain_match(member, record, profile)
            alumni_out.append(
                AlumnusOut(
                    id=record.id,
                    cluster_id=cluster.id,
                    similarity=member.total,
                    class_year=record.graduation_year,
                    match_reason=reason.summary if reason else None,
                    majors=record.final_majors,
                    programs=[
                        ProgramOut(code=p.code, role=p.role, provenance=p.provenance)
                        for p in program_views(record)
                    ],
                    outcome=build_outcome(record),
                )
            )

    response = ConstellationResponse(
        student=StudentContext(
            year=_year_value(profile),
            interests=profile.interests,
            courses=[profile.course_names[c] for c in profile.course_codes],
        ),
        clusters=[
            ClusterOut(
                id=c.id,
                label=c.label,
                similarity=c.similarity,
                top_majors=c.top_majors,
                member_count=c.member_count,
            )
            for c in clusters
        ],
        alumni=alumni_out,
        cluster_edges=[
            ClusterEdgeOut(source=e.source, target=e.target, weight=e.weight) for e in edges
        ],
        meta=ConstellationMeta(
            cached=cached,
            generated_at=datetime.now(UTC).isoformat(),
            total_candidates=total_candidates,
            returned=len(alumni_out),
            edges_before_pruning=edges_before_pruning,
        ),
    )
    return response, scored


def _year_value(profile: StudentProfile) -> str:
    from app.models import StudentYear

    return StudentYear.from_index(profile.year_index).value
