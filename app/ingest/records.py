"""Source-neutral records — the contract every data source produces.

This is the seam that makes the underlying dataset replaceable. A source
adapter's only job is to turn whatever it holds (MIDFIELD CSVs today, a real
registrar export tomorrow) into these plain dataclasses. The loader consumes
*only* these, and knows nothing about where they came from.

Field-for-field these mirror the ORM models in `app.models.academic`, but they
are deliberately decoupled: no SQLAlchemy, no session, no I/O. That keeps the
transform logic in each adapter unit-testable without a database, and means a
schema change on the DB side is a change in exactly one place (the loader),
not scattered across every source.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CourseRecord:
    code: str
    name: str
    semester_index: int
    dropped: bool = False
    discipline: str | None = None
    credit_hours: float | None = None


@dataclass
class MajorRecord:
    """A program assignment. `role`/`provenance` are strings here (the ingest
    contract stays dependency-light); the loader maps them to the DB enums."""

    name: str
    declared_semester: int
    is_final: bool = False
    role: str = "primary"
    provenance: str = "reported"
    cip6: str | None = None


@dataclass
class PivotRecord:
    semester_index: int
    from_major: str
    to_major: str
    note: str | None = None


@dataclass
class MilestoneRecord:
    semester_index: int
    text: str


@dataclass
class AlumnusRecord:
    """One graduate: a full transcript plus where they ended up.

    `career_area` is the clustering axis. When a source has genuine employment
    outcomes it belongs here; MIDFIELD has none, so its adapter fills this with
    the academic discipline of the final degree and says so in `outcome_title`.
    """

    id: str
    graduation_year: int
    outcome_title: str
    outcome_org: str
    career_area: str
    interests: list[str] = field(default_factory=list)
    courses: list[CourseRecord] = field(default_factory=list)
    majors: list[MajorRecord] = field(default_factory=list)
    pivots: list[PivotRecord] = field(default_factory=list)
    milestones: list[MilestoneRecord] = field(default_factory=list)


@dataclass
class StudentRecord:
    """A current student — the query side of the matching engine.

    `year` is a `StudentYear` value ("freshman".."senior"). `intended_direction`
    is optional: absent means a broad-explore query rather than a targeted
    What If.
    """

    id: str
    year: str
    declared_major: str | None = None
    intended_direction: str | None = None
    interests: list[str] = field(default_factory=list)
    courses: list[CourseRecord] = field(default_factory=list)
