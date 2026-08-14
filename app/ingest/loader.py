"""Load source-neutral records into Postgres.

This is the single translation point between the ingest contract and the ORM.
It is source-agnostic on purpose: give it any `SourceAdapter` and it writes the
same tables the API reads. Swapping datasets never touches this file.

After a load, run the recompute job (`python -m app.jobs.recompute`, or
`POST /api/admin/recompute`) to rebuild the cached constellations.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.ingest.base import SourceAdapter
from app.ingest.records import AlumnusRecord, StudentRecord
from app.models import (
    Alumnus,
    AlumnusCourse,
    AlumnusMajor,
    Milestone,
    Pivot,
    ProgramRole,
    Provenance,
    Student,
    StudentCourse,
    StudentProgram,
    StudentYear,
)

log = structlog.get_logger(__name__)


@dataclass
class LoadStats:
    alumni: int = 0
    students: int = 0
    courses: int = 0
    pivots: int = 0


def _alumnus_to_orm(rec: AlumnusRecord, school_id: str) -> Alumnus:
    alumnus = Alumnus(
        id=rec.id,
        school_id=school_id,
        graduation_year=rec.graduation_year,
        outcome_title=rec.outcome_title,
        outcome_org=rec.outcome_org,
        career_area=rec.career_area,
        interests=list(rec.interests),
    )
    alumnus.courses = [
        AlumnusCourse(
            course_code=c.code,
            course_name=c.name,
            semester_index=c.semester_index,
            dropped=c.dropped,
            discipline=c.discipline,
            credit_hours=c.credit_hours,
        )
        for c in rec.courses
    ]
    alumnus.majors = [
        AlumnusMajor(
            name=m.name,
            cip6=m.cip6,
            declared_semester=m.declared_semester,
            is_final=m.is_final,
            role=ProgramRole(m.role),
            provenance=Provenance(m.provenance),
        )
        for m in rec.majors
    ]
    alumnus.pivots = [
        Pivot(
            semester_index=p.semester_index,
            from_major=p.from_major,
            to_major=p.to_major,
            note=p.note,
        )
        for p in rec.pivots
    ]
    alumnus.milestones = [
        Milestone(semester_index=m.semester_index, text=m.text) for m in rec.milestones
    ]
    return alumnus


def _student_to_orm(rec: StudentRecord, school_id: str) -> Student:
    student = Student(
        id=rec.id,
        school_id=school_id,
        year=StudentYear(rec.year),
        # Scalar kept as a mirror of the primary program while the column exists.
        declared_major=rec.declared_major,
        intended_direction=rec.intended_direction,
        interests=list(rec.interests),
    )
    student.courses = [
        StudentCourse(course_code=c.code, course_name=c.name, semester_index=c.semester_index)
        for c in rec.courses
    ]
    # The major lives in the join table too, so accessors stay set-based.
    if rec.declared_major:
        student.programs = [
            StudentProgram(name=rec.declared_major, role=ProgramRole.primary, term=0)
        ]
    return student


async def reset_corpus(session: AsyncSession) -> None:
    """Delete every student and alumnus (child rows cascade).

    Ingestion replaces the corpus wholesale rather than merging, so a reload
    from a corrected source can't leave orphaned records from a previous run.

    Schools survive a reset on purpose: students who registered through the app
    reference them, and dropping a school would cascade those accounts away.
    """
    await session.execute(delete(Student))
    await session.execute(delete(Alumnus))
    await session.commit()


class _Schools:
    """Resolves institution names to school ids, creating rows as needed.

    Memoized because a load walks tens of thousands of records across a handful
    of institutions — one `get_or_create` per record would be a round trip per
    row for an answer that never changes.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._by_name: dict[str, str] = {}

    async def resolve(self, name: str | None) -> str:
        key = (name or repository.DEFAULT_SCHOOL_NAME).strip() or repository.DEFAULT_SCHOOL_NAME
        if key not in self._by_name:
            school = await repository.get_or_create_school(self._session, key)
            self._by_name[key] = school.id
        return self._by_name[key]


async def load(
    adapter: SourceAdapter,
    session: AsyncSession,
    *,
    reset: bool = True,
    batch_size: int = 500,
) -> LoadStats:
    """Stream an adapter's records into Postgres.

    Commits in batches so a large corpus doesn't build one giant transaction.
    With `reset=True` the existing corpus is cleared first.
    """
    stats = LoadStats()

    if reset:
        await reset_corpus(session)

    schools = _Schools(session)

    pending = 0
    for rec in adapter.alumni():
        orm = _alumnus_to_orm(rec, await schools.resolve(rec.school_name))
        session.add(orm)
        stats.alumni += 1
        stats.courses += len(orm.courses)
        stats.pivots += len(orm.pivots)
        pending += 1
        if pending >= batch_size:
            await session.commit()
            pending = 0
    if pending:
        await session.commit()

    pending = 0
    for rec in adapter.students():
        session.add(_student_to_orm(rec, await schools.resolve(rec.school_name)))
        stats.students += 1
        pending += 1
        if pending >= batch_size:
            await session.commit()
            pending = 0
    if pending:
        await session.commit()

    log.info(
        "ingest_loaded",
        source=adapter.name,
        alumni=stats.alumni,
        students=stats.students,
        courses=stats.courses,
        pivots=stats.pivots,
    )
    return stats
