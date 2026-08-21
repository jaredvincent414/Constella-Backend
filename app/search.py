"""Typeahead search — the pure half of the topbar's "Search paths, majors…" box.

Three result kinds, because three things on the map are worth jumping to: a
**major**, a **career cluster**, and an **alumnus path**. The first two are
destinations — picking one narrows Explore — while an alumnus is one person out
of many, which is why they sort last: the first row is what Enter selects.

An alumnus matches on **course code only**. Majors and career areas already have
result kinds of their own, and an alumnus row that merely restates the major the
user just typed is noise beside the major row itself. A course code is the one
thing no other kind can represent, and "who took ORGO 201" is a question the
constellation cannot otherwise answer.

Nothing here touches a session: the SQL lives in `app/repository.py` and
everything in this module is pure, so it runs in tests without Postgres.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.matching.clustering import slugify
from app.matching.programs import final_majors, origin_majors
from app.matching.timeline import course_display_name
from app.models import Alumnus

KIND_MAJOR = "major"
KIND_CLUSTER = "cluster"
KIND_ALUMNUS = "alumnus"

# Destinations before people. A major or a cluster is a place the student can go
# and a filter they can apply; an alumnus is a single path among hundreds that
# happens to contain the course they typed.
_KIND_ORDER = {KIND_MAJOR: 0, KIND_CLUSTER: 1, KIND_ALUMNUS: 2}

# Below three characters an `ILIKE '%q%'` pattern contains no whole trigram, so
# the GIN indexes on `alumni.career_area`, `alumnus_courses.course_code`, and
# `alumnus_majors.name` cannot be used and every keystroke becomes a full scan —
# for a prefix that matches most of the corpus anyway. Short queries answer empty.
MIN_QUERY_LENGTH = 3

_WHITESPACE = re.compile(r"\s+")

# Backslash is Postgres's default LIKE escape character, so escaping with it
# needs no ESCAPE clause.
_LIKE_SPECIALS = str.maketrans({"\\": r"\\", "%": r"\%", "_": r"\_"})


@dataclass(frozen=True)
class SearchHit:
    """One row of the dropdown, before it becomes a response model.

    `score` is pg_trgm's `similarity()` against the query. It orders the rows and
    is deliberately not on the wire: it is a ranking detail the frontend has no
    use for, and shipping it invites rendering a second percentage beside the
    constellation's very differently-derived match score.
    """

    kind: str
    id: str
    label: str
    detail: str | None
    score: float
    count: int | None = None
    provenance: str | None = None


def normalize_query(raw: str | None) -> str:
    """Trim and collapse whitespace. A typeahead fires mid-word, so the string
    arrives with whatever spacing the user has typed so far."""
    if not raw:
        return ""
    return _WHITESPACE.sub(" ", raw).strip()


def like_pattern(query: str) -> str:
    """The `ILIKE` pattern for a user-typed query, wildcards escaped.

    A literal `%` typed into the box has to match a percent sign. Left unescaped
    it turns one keystroke into a full corpus dump — the same class of mistake as
    an unparameterized query, minus the write.
    """
    return f"%{query.translate(_LIKE_SPECIALS)}%"


def matches(query: str, value: str) -> bool:
    """The Python equivalent of the SQL filter: `ILIKE '%query%'` with the
    wildcards escaped is exactly a case-insensitive substring test."""
    return query.lower() in value.lower()


def _alumni_count(count: int) -> str:
    return "1 alumnus" if count == 1 else f"{count} alumni"


def major_hit(name: str, count: int, score: float) -> SearchHit:
    """The dropdown row for a major.

    `id` is the slug, a stable key for the list; the value to hand back to
    Explore's `?major=` facet is `label`, which is free text on that endpoint.
    """
    return SearchHit(
        kind=KIND_MAJOR,
        id=slugify(name),
        label=name,
        detail=_alumni_count(count),
        score=score,
        count=count,
    )


def cluster_hit(label: str, count: int, score: float, provenance: str | None) -> SearchHit:
    """The dropdown row for a career cluster.

    `id` is `slugify(label)` — the same id `build_clusters` assigns — so a row
    selected here names the cluster the constellation already drew, rather than
    a second identifier the frontend would have to reconcile.
    """
    return SearchHit(
        kind=KIND_CLUSTER,
        id=slugify(label),
        label=label,
        detail=_alumni_count(count),
        score=score,
        count=count,
        provenance=provenance,
    )


def alumnus_label(alumnus: Alumnus) -> str:
    """Alumni have no name column by construction, so the class year is the whole
    identity the frontend renders."""
    return f"Class of {alumnus.graduation_year}"


def _program_phrase(alumnus: Alumnus) -> str:
    origin = " + ".join(sorted(origin_majors(alumnus)))
    final = " + ".join(sorted(final_majors(alumnus)))
    if origin and final and origin != final:
        return f"{origin} → {final}"
    return origin or final


def _matched_course(alumnus: Alumnus, query: str) -> str | None:
    """Which course to show. Earliest matching term wins — arbitrary but stable,
    so the same query never renders the same alumnus two different ways."""
    ordered = sorted(alumnus.courses, key=lambda c: (c.semester_index, c.course_code))
    for course in ordered:
        if matches(query, course.course_code):
            return course_display_name(course)
    return None


def alumnus_hit(alumnus: Alumnus, score: float, query: str) -> SearchHit:
    """The dropdown row for an alumnus matched on a course code.

    The detail line names their majors and the course that matched, and stops
    there. It carries no career outcome on purpose: the outcome is
    `provenance='synthetic'` on the placeholder dataset, and a one-line search
    hint has nowhere to put the flag that says so. The detail panel this row
    opens shows the outcome beside its provenance, which is where it belongs.
    """
    parts = [p for p in (_program_phrase(alumnus), _matched_course(alumnus, query)) if p]
    return SearchHit(
        kind=KIND_ALUMNUS,
        id=alumnus.id,
        label=alumnus_label(alumnus),
        detail=" · ".join(parts) or None,
        score=score,
    )


def order_hits(hits: list[SearchHit]) -> list[SearchHit]:
    """Kind first, then similarity, then a total tie-break.

    Ties are the common case — every alumnus who took the matched course scores
    identically — so the ordering has to be total, or the same query returns the
    same rows in a different order on the next keystroke.
    """
    return sorted(
        hits,
        key=lambda h: (_KIND_ORDER.get(h.kind, len(_KIND_ORDER)), -h.score, h.label, h.id),
    )
