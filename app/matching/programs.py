"""Program (major/minor) accessors and pivot detection.

Every read of "what are this person's majors" goes through here. No other module
reconstructs a major set by walking `.majors`/`.programs` and reading `.name` —
that is exactly how the single-major assumption creeps back in, and acceptance
test 6.5 greps for it. The accessors always return a **set**, even for one
program.

Each accessor takes either an entity (an `Alumnus` with `.majors` or a `Student`
with `.programs`) or a raw list of program rows — so callers pass `alumnus`, not
`alumnus.majors`, and tests can pass a hand-built list. Nothing here touches a
session: repositories eager-load and the matching engine operates on plain
objects, so an accessor doing IO would break that contract.

Program identity is the program **name**, on both sides of every comparison. It
is deliberately *not* the CIP code even where one exists — see `_key` for why a
symmetric key matters more here than a precise one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.models import MAJOR_ROLES, ProgramRole, Provenance

# Larger than any real semester index; marks an interval that runs to graduation.
_OPEN = 10_000


class ProgramRow(Protocol):
    """The shape shared by AlumnusMajor and StudentProgram."""

    name: str
    role: ProgramRole
    is_final: bool


def _rows(source) -> list[ProgramRow]:
    """Resolve an entity or a row list to a list of program rows.

    Accepts an `Alumnus` (`.majors`), a `Student` (`.programs`), or an already
    materialized list — so callers never have to reach into the relationship
    themselves."""
    majors = getattr(source, "majors", None)
    if majors is not None:
        return list(majors)
    programs = getattr(source, "programs", None)
    if programs is not None:
        return list(programs)
    return list(source)


def _role(row: ProgramRow) -> ProgramRole:
    """A row's role, defaulting to `primary`.

    Column defaults apply at flush time, so a transient (unsaved) row — as built
    in tests or in-memory — can have `role=None`. Treat that as a primary major
    rather than dropping it from every set.
    """
    return row.role or ProgramRole.primary


def _is_final(row: ProgramRow) -> bool:
    """Whether a program runs to graduation. `StudentProgram` rows have no such
    column — a current student's programs are active now — so they default True."""
    return getattr(row, "is_final", True)


def _key(row: ProgramRow) -> str:
    """Set element for a program — its **name**.

    Name rather than `cip6`, even though CIP is the more precise identifier,
    because CIP is populated *asymmetrically*: MIDFIELD (and any registrar
    export) carries it on `alumnus_majors` but never on `student_program`, which
    the app writes from free text. Keying on "cip6 if present" therefore put the
    two sides of every comparison in different key spaces — the element kernel
    was evaluating `text_similarity("Engineering", "140102")`, which is 0 — and
    that silently zeroed the entire 20% major-match component on real data. It
    also left `derive_minors` unable to recognize a declared minor, since it
    matches discipline names against this set.

    `name` is non-nullable on both tables, so it is the only key both sides
    always have. A symmetric key beats a more precise one that only one side
    populates.

    When a source does supply `cip6` on both sides, the place for CIP equality is
    the comparison kernel (`scoring.soft_jaccard`) as a fast path above name
    similarity — not here, where it silently changes what identity *means*
    depending on the source.
    """
    return row.name or getattr(row, "cip6", None) or ""


def _term(row: ProgramRow) -> int:
    """The term a program assignment holds for. Alumni use `declared_semester`;
    student rows use `term`. Both mean the same thing here."""
    value = getattr(row, "declared_semester", None)
    return value if value is not None else getattr(row, "term", 0)


def _intervals(rows: list[ProgramRow], roles) -> list[tuple[str, int, int]]:
    """Reconstruct [start, end) activity intervals for each program of `roles`.

    A program is active from the term it's declared until either graduation (if
    it's final) or the term a *different* program of the same roles is declared
    that replaces it. This turns the sparse "declared at" rows into a per-term
    picture without assuming one major per term — and it's exact when a source
    supplies genuine per-term rows (each term is its own change point).
    """
    ordered = sorted((r for r in rows if _role(r) in roles), key=lambda r: (_term(r), _key(r)))
    intervals: list[tuple[str, int, int]] = []
    for i, row in enumerate(ordered):
        start = _term(row)
        if _is_final(row):
            end = _OPEN
        else:
            end = next(
                (_term(other) for other in ordered[i + 1:] if _key(other) != _key(row)),
                _OPEN,
            )
        intervals.append((_key(row), start, end))
    return intervals


def _active(rows: list[ProgramRow], roles, term: int) -> set[str]:
    return {key for key, start, end in _intervals(rows, roles) if start <= term < end}


def _change_terms(rows: list[ProgramRow], roles) -> list[int]:
    return sorted({_term(r) for r in rows if _role(r) in roles})


def majors_at(source, term: int) -> set[str]:
    """The set of majors (primary + second major) active at `term`."""
    return _active(_rows(source), MAJOR_ROLES, term)


def minors_at(source, term: int) -> set[str]:
    """The set of minors active at `term`."""
    return _active(_rows(source), {ProgramRole.minor}, term)


def origin_majors(source) -> set[str]:
    """Majors held at the earliest declared term — the 'from' side of a pivot."""
    rows = _rows(source)
    terms = _change_terms(rows, MAJOR_ROLES)
    return _active(rows, MAJOR_ROLES, terms[0]) if terms else set()


def final_majors(source) -> set[str]:
    """Majors held at graduation. Prefers `is_final`, else the last declared."""
    rows = _rows(source)
    finals = {_key(r) for r in rows if _role(r) in MAJOR_ROLES and _is_final(r)}
    if finals:
        return finals
    terms = _change_terms(rows, MAJOR_ROLES)
    return _active(rows, MAJOR_ROLES, terms[-1]) if terms else set()


def all_minors(source) -> set[str]:
    """Every minor ever held (declared/derived), regardless of term."""
    return {_key(r) for r in _rows(source) if _role(r) == ProgramRole.minor}


def student_majors(student) -> set[str]:
    """A current student's majors as a set.

    Reads their `student_program` rows; falls back to the deprecated
    `declared_major` scalar only while that column still exists. This is the one
    place the fallback lives, so callers stay set-based.
    """
    rows = _rows(student)
    if rows:
        return _active(rows, MAJOR_ROLES, max(_term(p) for p in rows))
    declared = getattr(student, "declared_major", None)
    return {declared} if declared else set()


def student_minors(student) -> set[str]:
    rows = _rows(student)
    if rows:
        return _active(rows, {ProgramRole.minor}, max(_term(p) for p in rows))
    return set()


@dataclass(frozen=True)
class ProgramPivot:
    """A term where the program set changed, and how."""

    term: int
    kind: str  # "added" | "dropped" | "switched"
    role: str  # "major" | "minor" — which set changed
    added: frozenset[str]
    removed: frozenset[str]


def _classify(prev: set[str], cur: set[str]) -> str:
    if cur > prev:
        return "added"
    if cur < prev:
        return "dropped"
    return "switched"


def _diff_timeline(
    rows: list[ProgramRow], roles, role_label: str, matriculation: int
) -> list[ProgramPivot]:
    pivots: list[ProgramPivot] = []
    prev: set[str] = set()
    for term in _change_terms(rows, roles):
        cur = _active(rows, roles, term)
        if cur != prev:
            # Arriving with a program at matriculation isn't a pivot; picking one
            # up *later* (e.g. a minor added junior year) is.
            if prev or term != matriculation:
                pivots.append(
                    ProgramPivot(
                        term=term,
                        kind=_classify(prev, cur),
                        role=role_label,
                        added=frozenset(cur - prev),
                        removed=frozenset(prev - cur),
                    )
                )
            prev = cur
    return pivots


def detect_pivots(source) -> list[ProgramPivot]:
    """All pivots — major-set and minor-set changes — as set differences.

    `added`/`dropped`/`switched` are kept distinct so the What-If Simulator can
    tell "added a minor" from "switched majors". A term where only minors
    changed is its own pivot, tagged `role='minor'`.
    """
    rows = _rows(source)
    matriculation = min((_term(r) for r in rows), default=0)
    pivots = _diff_timeline(rows, MAJOR_ROLES, "major", matriculation)
    pivots += _diff_timeline(rows, {ProgramRole.minor}, "minor", matriculation)
    return sorted(pivots, key=lambda p: (p.term, p.role))


def first_major_pivot(source) -> ProgramPivot | None:
    """The earliest major-set change, or None. Used to locate the pivot term."""
    return next((p for p in detect_pivots(source) if p.role == "major"), None)


def _provenance(row: ProgramRow) -> str:
    return (getattr(row, "provenance", None) or Provenance.reported).value


@dataclass(frozen=True)
class ProgramView:
    """A program as the API exposes it — `provenance` lets the UI label a
    `derived` minor as inferred rather than formally declared."""

    code: str
    role: str  # "major" | "minor"
    provenance: str


def program_views(source) -> list[ProgramView]:
    """The graduation majors and all minors, each tagged with provenance.

    `code` is the human-readable program name for display, matching the `majors`
    field and the key the accessors above return.
    """
    rows = _rows(source)
    provenance_of: dict[str, str] = {}
    name_of: dict[str, str] = {}
    for row in rows:
        provenance_of.setdefault(_key(row), _provenance(row))
        name_of.setdefault(_key(row), row.name)
    majors = sorted(final_majors(rows))
    minors = sorted(all_minors(rows))
    views = [
        ProgramView(name_of.get(k, k), "major", provenance_of.get(k, "reported")) for k in majors
    ]
    views += [
        ProgramView(name_of.get(k, k), "minor", provenance_of.get(k, "reported")) for k in minors
    ]
    return views


# Re-exported for callers that want the iterable type hint.
__all__ = [
    "ProgramPivot",
    "ProgramRow",
    "ProgramView",
    "all_minors",
    "detect_pivots",
    "final_majors",
    "first_major_pivot",
    "majors_at",
    "minors_at",
    "origin_majors",
    "program_views",
    "student_majors",
    "student_minors",
]
