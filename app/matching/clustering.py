"""Clustering and cluster-edge construction.

Alumni are grouped by career outcome before the response leaves the server, so
the frontend renders pre-assigned groups rather than raw data.

Why career area and not job title: titles are too granular to group on —
"Policy Analyst", "Analyst II", and "Senior Health Analyst" are three clusters
of one. The career area is the level at which "distance encodes similarity"
means something to a student.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from app.matching.outcomes import outcome_industry
from app.matching.scoring import ScoredAlumnus
from app.matching.text import normalize

# The frontend's radial layout allocates an angular sector per cluster; past
# roughly ten the sectors get too thin to label. Clusters beyond this are
# dropped (weakest first) rather than rendered illegibly.
DEFAULT_MAX_CLUSTERS = 10

TOP_MAJORS_PER_CLUSTER = 3


@dataclass
class Cluster:
    id: str
    label: str
    similarity: float
    top_majors: list[str]
    members: list[ScoredAlumnus] = field(default_factory=list)
    major_set: set[str] = field(default_factory=set)

    @property
    def member_count(self) -> int:
        return len(self.members)


@dataclass
class ClusterEdge:
    source: str
    target: str
    weight: float


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "cluster"


def build_clusters(
    scored: list[ScoredAlumnus],
    max_clusters: int = DEFAULT_MAX_CLUSTERS,
) -> list[Cluster]:
    """Group scored alumni by career area.

    Cluster similarity is the **mean** of its members' scores, not the max. A
    cluster containing one perfect match and eleven poor ones is not a strong
    cluster, and radius is drawn from this number — the mean is what makes
    proximity honest.
    """
    grouped: dict[str, list[ScoredAlumnus]] = {}
    labels: dict[str, str] = {}

    for item in scored:
        industry = outcome_industry(item.alumnus)
        key = normalize(industry) or "unspecified"
        grouped.setdefault(key, []).append(item)
        # Keep the first-seen surface form so the label reads naturally.
        labels.setdefault(key, industry)

    clusters: list[Cluster] = []
    for key, members in grouped.items():
        members.sort(key=lambda s: (-s.total, s.alumnus.id))
        mean_similarity = sum(m.total for m in members) / len(members)

        major_counts: Counter[str] = Counter()
        major_set: set[str] = set()
        for member in members:
            for major in member.alumnus.final_majors:
                major_counts[major] += 1
                major_set.add(normalize(major))

        clusters.append(
            Cluster(
                id=slugify(labels[key]),
                label=labels[key],
                similarity=round(mean_similarity, 4),
                top_majors=[m for m, _ in major_counts.most_common(TOP_MAJORS_PER_CLUSTER)],
                members=members,
                major_set=major_set,
            )
        )

    clusters.sort(key=lambda c: (-c.similarity, c.id))
    if max_clusters > 0:
        clusters = clusters[:max_clusters]
    return clusters


def build_cluster_edges(
    clusters: list[Cluster],
    min_weight: float,
    max_count: int,
) -> tuple[list[ClusterEdge], int]:
    """Shared-major relationships between clusters, pruned.

    Left unfiltered this hairballs: if eight clusters all contain a Biology
    major you get a near-complete graph. The frontend renders only edges at or
    above `min_weight`, capped at the top `max_count` by weight — this applies
    the same rules server-side so we aren't shipping edges that get discarded.

    Returns the pruned edges and the pre-pruning count (for diagnostics).
    """
    candidates: list[ClusterEdge] = []
    for i, source in enumerate(clusters):
        for target in clusters[i + 1 :]:
            union = source.major_set | target.major_set
            if not union:
                continue
            weight = len(source.major_set & target.major_set) / len(union)
            if weight <= 0.0:
                continue
            candidates.append(
                ClusterEdge(source=source.id, target=target.id, weight=round(weight, 4))
            )

    total_before_pruning = len(candidates)

    kept = [edge for edge in candidates if edge.weight >= min_weight]
    kept.sort(key=lambda e: (-e.weight, e.source, e.target))
    if max_count > 0:
        kept = kept[:max_count]
    return kept, total_before_pruning
