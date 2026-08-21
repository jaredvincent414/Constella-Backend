"""The precompute pipeline.

    score every alumnus  ->  rank  ->  cut to top-N  ->  cluster  ->  prune edges

This is the "background job" box in the spec's data flow diagram. It is written
as a pure function over already-loaded ORM objects so it can run either inside a
job or inline on a cache miss, without knowing which.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from app.config import settings
from app.matching.clustering import DEFAULT_MAX_CLUSTERS, build_cluster_edges, build_clusters
from app.matching.corpus import Corpus, as_corpus
from app.matching.explain import explain_match
from app.matching.facets import filter_by_facets
from app.matching.outcomes import build_outcome
from app.matching.programs import program_views
from app.matching.scoring import (
    ScoredAlumnus,
    StudentProfile,
    filter_by_pivot_query,
    score_corpus,
)
from app.matching.timeline import semester_label
from app.models import Alumnus
from app.schemas import (
    AlumnusOut,
    ClusterEdgeOut,
    ClusterOut,
    ConstellationMeta,
    ConstellationResponse,
    PivotPointOut,
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
    per-alumnus breakdowns doesn't have to score twice. That list is the *kept*
    top-N, not the whole corpus — the cut happens inside the scorer now, so the
    records below it are never materialized.
    """
    limit = max_alumni or settings.constellation_max_alumni

    corpus = as_corpus(alumni)
    total_candidates = len(corpus)
    top = score_corpus(profile, corpus, top_n=limit)
    scored = top

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
    clusters_out: list[ClusterOut] = []
    returned = 0
    for cluster in clusters:
        members: list[AlumnusOut] = []
        for member in cluster.members:
            record = member.alumnus
            reason = explain_match(member, record, profile)
            programs = program_views(record)
            members.append(
                AlumnusOut(
                    id=record.id,
                    # 0..1 internally, 0..100 on the wire: the frontend renders
                    # this as a match percentage and sizes nodes by it, and one
                    # scale for both beats each side remembering to convert.
                    similarity_score=round(member.total * 100, 1),
                    graduation_year=record.graduation_year,
                    cluster=cluster.label,
                    match_reason=reason.summary if reason else None,
                    majors=record.final_majors,
                    minors=[p.code for p in programs if p.role == "minor"],
                    programs=[
                        ProgramOut(code=p.code, role=p.role, provenance=p.provenance)
                        for p in programs
                    ],
                    career_outcome=build_outcome(record),
                    interests=list(record.interests or []),
                    pivot_points=[
                        PivotPointOut(
                            semester=semester_label(pivot.semester_index),
                            from_major=pivot.from_major,
                            to_major=pivot.to_major,
                        )
                        for pivot in sorted(record.pivots, key=lambda p: p.semester_index)
                    ],
                )
            )
        returned += len(members)
        clusters_out.append(
            ClusterOut(
                id=cluster.id,
                label=cluster.label,
                similarity=cluster.similarity,
                top_majors=cluster.top_majors,
                alumni=members,
            )
        )

    response = ConstellationResponse(
        student=StudentContext(
            year=_year_value(profile),
            interests=profile.interests,
            courses=[profile.course_names[c] for c in profile.course_codes],
        ),
        clusters=clusters_out,
        cluster_edges=[
            ClusterEdgeOut(source=e.source, target=e.target, weight=e.weight) for e in edges
        ],
        total_alumni=returned,
        summary=_summary(returned, len(clusters_out)),
        meta=ConstellationMeta(
            cached=cached,
            generated_at=datetime.now(UTC).isoformat(),
            total_candidates=total_candidates,
            returned=returned,
            edges_before_pruning=edges_before_pruning,
        ),
    )
    return response, scored


def build_constellation_for_query(
    profile: StudentProfile,
    alumni: list[Alumnus] | Corpus,
    from_major: str | None = None,
    to_major: str | None = None,
    max_alumni: int | None = None,
    interests: list[str] | None = None,
    career_area: str | None = None,
    major: str | None = None,
) -> ConstellationResponse:
    """`build_constellation` with the Explore query applied first.

    The request path and the nightly job both cache under a key derived from
    this query, so they have to agree on what that key *means* down to the last
    detail — a job that filtered differently would warm an entry the route would
    never have produced, and the route would serve it. Sharing one function is
    what makes them agree by construction rather than by two copies staying in
    sync.

    Facets narrow, the pivot query narrows, and only then does scoring run — so
    the top-N cut is taken from the eligible set rather than from the whole
    corpus. The profile is copied, not mutated: the job walks several queries
    for one student, and an override leaking from one variant into the next
    would score them against the wrong hypothetical.
    """
    corpus = as_corpus(alumni)
    corpus = filter_by_facets(corpus, interests, career_area, major)
    if to_major:
        corpus = filter_by_pivot_query(corpus, from_major, to_major)

    if from_major:
        profile = replace(profile, declared_major=from_major, current_majors={from_major})
    if to_major:
        profile = replace(profile, intended_direction=to_major)

    response, _ = build_constellation(profile, corpus, max_alumni=max_alumni)
    return response


def _summary(alumni: int, clusters: int) -> str:
    """The header line. Built here so every client says the same thing about the
    same payload, rather than each one counting for itself."""
    if not alumni:
        return "No alumni matched this search"
    return (
        f"{alumni} alumn{'us' if alumni == 1 else 'i'} "
        f"across {clusters} career cluster{'' if clusters == 1 else 's'}"
    )


def _year_value(profile: StudentProfile) -> str:
    from app.models import StudentYear

    return StudentYear.from_index(profile.year_index).value
