"""ORM models for students, alumni, and their academic records.

Semester encoding
-----------------
Every course, major declaration, and pivot carries a `semester_index` in 0..7:

    0 Freshman Fall    2 Sophomore Fall   4 Junior Fall    6 Senior Fall
    1 Freshman Spring  3 Sophomore Spring 5 Junior Spring  7 Senior Spring

A single integer makes the two operations the scoring engine cares about into
arithmetic: "courses before the pivot" is a comparison, and "which year did this
happen in" is `semester_index // 2`. Storing labels like "Sophomore Spring" as
the source of truth would force string parsing in the hot path.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

SEMESTERS_PER_YEAR = 2
TOTAL_SEMESTERS = 8

YEAR_LABELS = ["Freshman", "Sophomore", "Junior", "Senior"]
TERM_LABELS = ["Fall", "Spring"]


class StudentYear(enum.StrEnum):
    """Matches the frontend's `student.year` union exactly."""

    freshman = "freshman"
    sophomore = "sophomore"
    junior = "junior"
    senior = "senior"

    @property
    def index(self) -> int:
        return list(StudentYear).index(self)

    @classmethod
    def from_index(cls, index: int) -> StudentYear:
        return list(cls)[max(0, min(index, len(list(cls)) - 1))]


class ProgramRole(enum.StrEnum):
    """A program assignment's kind. `primary` + `second_major` are what count as
    "majors" for matching; `minor`/`concentration` are secondary."""

    primary = "primary"
    second_major = "second_major"
    minor = "minor"
    concentration = "concentration"


# The roles that constitute a student's *majors* — the set the similarity engine
# compares. Defined once here so no caller re-hardcodes the membership.
MAJOR_ROLES = frozenset({ProgramRole.primary, ProgramRole.second_major})


class Provenance(enum.StrEnum):
    """Where a program row came from. `reported` is from the source of record;
    `derived` is inferred by us (e.g. a de facto minor) and must be labeled as
    inferred anywhere it surfaces in the UI."""

    reported = "reported"
    derived = "derived"


def semester_label(semester_index: int) -> str:
    """0 -> 'Freshman Fall', 3 -> 'Sophomore Spring'."""
    year = YEAR_LABELS[min(semester_index // SEMESTERS_PER_YEAR, len(YEAR_LABELS) - 1)]
    term = TERM_LABELS[semester_index % SEMESTERS_PER_YEAR]
    return f"{year} {term}"


class Student(Base):
    __tablename__ = "students"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    year: Mapped[StudentYear] = mapped_column(
        Enum(StudentYear, name="student_year", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    # DEPRECATED scalar — a student's majors now live in `student_program`
    # (role='primary'). Kept for one release so the backfill can be verified
    # before it's dropped; no code reads it (see app/matching/programs.py).
    declared_major: Mapped[str | None] = mapped_column(String(128))
    # The "to" half of a pivot query: where the student wants to end up. Free
    # text so it can hold either a major ("Public Health") or a career area
    # ("Health Policy") — the scorer matches against both.
    intended_direction: Mapped[str | None] = mapped_column(String(128))
    interests: Mapped[list[str]] = mapped_column(ARRAY(String(128)), default=list, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    courses: Mapped[list[StudentCourse]] = relationship(
        back_populates="student", cascade="all, delete-orphan", lazy="selectin"
    )
    programs: Mapped[list[StudentProgram]] = relationship(
        back_populates="student", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def year_index(self) -> int:
        return self.year.index


class StudentCourse(Base):
    __tablename__ = "student_courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[str] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True
    )
    course_code: Mapped[str] = mapped_column(String(32), nullable=False)
    course_name: Mapped[str] = mapped_column(String(160), nullable=False)
    semester_index: Mapped[int] = mapped_column(Integer, nullable=False)

    student: Mapped[Student] = relationship(back_populates="courses")

    __table_args__ = (
        Index("ix_student_courses_student_code", "student_id", "course_code", unique=True),
    )


class StudentProgram(Base):
    """A program (major/minor) a current student is enrolled in.

    Mirrors `AlumnusMajor` for the query side: one row per program per term, so a
    double-major current student is two `role='primary'`/`second_major` rows and
    never a scalar. See `ProgramRole` for the roles.
    """

    __tablename__ = "student_program"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[str] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True
    )
    term: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    cip6: Mapped[str | None] = mapped_column(String(6))
    role: Mapped[ProgramRole] = mapped_column(
        Enum(ProgramRole, name="program_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ProgramRole.primary,
    )
    provenance: Mapped[Provenance] = mapped_column(
        Enum(Provenance, name="provenance", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=Provenance.reported,
    )

    student: Mapped[Student] = relationship(back_populates="programs")

    __table_args__ = (
        Index("ix_student_program_unique", "student_id", "term", "name", "role", unique=True),
    )


class Alumnus(Base):
    __tablename__ = "alumni"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Deliberately no name column. The frontend renders "Class of 2022" and
    # never a real name, so the API has nothing to leak.
    graduation_year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    outcome_title: Mapped[str] = mapped_column(String(160), nullable=False)
    outcome_org: Mapped[str] = mapped_column(String(160), nullable=False)
    # The clustering key. Job titles are too granular to group on ("Analyst II"
    # vs "Policy Analyst"), so alumni cluster on the coarser career area.
    career_area: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    interests: Mapped[list[str]] = mapped_column(ARRAY(String(128)), default=list, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    courses: Mapped[list[AlumnusCourse]] = relationship(
        back_populates="alumnus", cascade="all, delete-orphan", lazy="selectin"
    )
    majors: Mapped[list[AlumnusMajor]] = relationship(
        back_populates="alumnus", cascade="all, delete-orphan", lazy="selectin"
    )
    pivots: Mapped[list[Pivot]] = relationship(
        back_populates="alumnus", cascade="all, delete-orphan", lazy="selectin"
    )
    milestones: Mapped[list[Milestone]] = relationship(
        back_populates="alumnus", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def first_pivot(self) -> Pivot | None:
        if not self.pivots:
            return None
        return min(self.pivots, key=lambda p: p.semester_index)

    @property
    def _major_programs(self) -> list[AlumnusMajor]:
        """Program rows that count as majors (excludes minors/concentrations).

        A transient row's `role` may be None (column defaults apply at flush),
        so an unset role counts as a primary major rather than vanishing.
        """
        return [m for m in self.majors if (m.role or ProgramRole.primary) in MAJOR_ROLES]

    @property
    def origin_major(self) -> str | None:
        """The major they started in — the 'from' side of a pivot query.

        Scalar for back-compat; `programs.origin_majors` is the set-valued form.
        """
        majors = self._major_programs
        if not majors:
            return None
        return min(majors, key=lambda m: m.declared_semester).name

    @property
    def final_majors(self) -> list[str]:
        """What they graduated with. Multiple entries mean a double major.

        Only major-role programs — a minor is never a graduation major.
        """
        majors = self._major_programs
        finals = [m.name for m in majors if m.is_final]
        if finals:
            return finals
        if not majors:
            return []
        last = max(majors, key=lambda m: m.declared_semester).declared_semester
        return [m.name for m in majors if m.declared_semester == last]

    def pre_pivot_courses(self) -> list[AlumnusCourse]:
        """Courses taken before they changed direction.

        The backend spec scores against the *pre-pivot* transcript: what they
        were taking while they were still where the student is now. An alumnus
        who never pivoted has no such boundary, so their whole transcript counts.
        """
        pivot = self.first_pivot
        if pivot is None:
            return list(self.courses)
        return [c for c in self.courses if c.semester_index < pivot.semester_index]


class AlumnusCourse(Base):
    __tablename__ = "alumnus_courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alumnus_id: Mapped[str] = mapped_column(
        ForeignKey("alumni.id", ondelete="CASCADE"), nullable=False, index=True
    )
    course_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    course_name: Mapped[str] = mapped_column(String(160), nullable=False)
    semester_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # True when the course was started and abandoned — drives the `[dropped]`
    # pill in the frontend's <AcademicTimeline>.
    dropped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Coarse subject area (e.g. "Engineering: Electrical and Computer"). Nullable
    # because not every source carries it; the derived-minor job reads it to spot
    # a concentration of coursework outside a student's declared field.
    discipline: Mapped[str | None] = mapped_column(String(160))
    credit_hours: Mapped[float | None] = mapped_column(Float)

    alumnus: Mapped[Alumnus] = relationship(back_populates="courses")

    __table_args__ = (
        Index("ix_alumnus_courses_alumnus_semester", "alumnus_id", "semester_index"),
    )


class AlumnusMajor(Base):
    """A program (major/minor) an alumnus held. `declared_semester` is the term
    the assignment holds for; multiple minors are simply multiple `role='minor'`
    rows. Never a scalar on the alumnus."""

    __tablename__ = "alumnus_majors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alumnus_id: Mapped[str] = mapped_column(
        ForeignKey("alumni.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 6-digit CIP code when the source provides one (registrar data will).
    # MIDFIELD stores names, so this stays null there and the name is the key.
    cip6: Mapped[str | None] = mapped_column(String(6))
    declared_semester: Mapped[int] = mapped_column(Integer, nullable=False)
    is_final: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    role: Mapped[ProgramRole] = mapped_column(
        Enum(ProgramRole, name="program_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ProgramRole.primary,
    )
    provenance: Mapped[Provenance] = mapped_column(
        Enum(Provenance, name="provenance", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=Provenance.reported,
    )

    alumnus: Mapped[Alumnus] = relationship(back_populates="majors")

    __table_args__ = (
        Index(
            "ix_alumnus_majors_unique",
            "alumnus_id",
            "declared_semester",
            "name",
            "role",
            unique=True,
        ),
    )


class Pivot(Base):
    __tablename__ = "pivots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alumnus_id: Mapped[str] = mapped_column(
        ForeignKey("alumni.id", ondelete="CASCADE"), nullable=False, index=True
    )
    semester_index: Mapped[int] = mapped_column(Integer, nullable=False)
    from_major: Mapped[str] = mapped_column(String(128), nullable=False)
    to_major: Mapped[str] = mapped_column(String(128), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    alumnus: Mapped[Alumnus] = relationship(back_populates="pivots")

    @property
    def year_index(self) -> int:
        return self.semester_index // SEMESTERS_PER_YEAR


class Milestone(Base):
    """A notable non-course event on the transcript.

    Renders as the optional `milestone` line on a timeline semester — thesis
    titles, internships, graduation.
    """

    __tablename__ = "milestones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alumnus_id: Mapped[str] = mapped_column(
        ForeignKey("alumni.id", ondelete="CASCADE"), nullable=False, index=True
    )
    semester_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(String(200), nullable=False)

    alumnus: Mapped[Alumnus] = relationship(back_populates="milestones")


class PrecomputeRun(Base):
    """Audit trail for background recompute jobs.

    Lets the API report when the cached constellation was last rebuilt, and
    makes a stuck or failing job visible instead of silently serving stale data.
    """

    __tablename__ = "precompute_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    student_id: Mapped[str | None] = mapped_column(String(64))
    alumni_scored: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    clusters_built: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ok", nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
