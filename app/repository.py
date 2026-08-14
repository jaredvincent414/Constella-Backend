"""Database access.

Kept separate from the routes so the matching engine can be exercised against
plain objects in tests without a live Postgres.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Alumnus, PrecomputeRun, SavedPath, Student


async def get_student(session: AsyncSession, student_id: str) -> Student | None:
    stmt = (
        select(Student)
        .where(Student.id == student_id)
        .options(selectinload(Student.courses))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_students(session: AsyncSession) -> list[Student]:
    stmt = select(Student).options(selectinload(Student.courses)).order_by(Student.id)
    return list((await session.execute(stmt)).scalars().all())


def _alumni_query():
    return select(Alumnus).options(
        selectinload(Alumnus.courses),
        selectinload(Alumnus.majors),
        selectinload(Alumnus.pivots),
        selectinload(Alumnus.milestones),
        selectinload(Alumnus.outcomes),
    )


async def list_alumni(session: AsyncSession) -> list[Alumnus]:
    """Load the whole corpus with relationships eager-loaded.

    The scorer touches every alumnus's courses, majors, and pivots, so lazy
    loading here would mean thousands of round trips per request. This is the
    query the background job is designed to amortize.
    """
    stmt = _alumni_query().order_by(Alumnus.id)
    return list((await session.execute(stmt)).scalars().unique().all())


async def get_alumnus(session: AsyncSession, alumnus_id: str) -> Alumnus | None:
    stmt = _alumni_query().where(Alumnus.id == alumnus_id)
    return (await session.execute(stmt)).scalars().unique().one_or_none()


async def count_alumni(session: AsyncSession) -> int:
    from sqlalchemy import func

    return (await session.execute(select(func.count(Alumnus.id)))).scalar_one()


async def list_saved_paths(session: AsyncSession, student_id: str) -> list[SavedPath]:
    stmt = (
        select(SavedPath)
        .where(SavedPath.student_id == student_id)
        .order_by(SavedPath.saved_at.desc(), SavedPath.id.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_saved_paths_by_ids(
    session: AsyncSession, student_id: str, path_ids: list[int]
) -> list[SavedPath]:
    """The student's saved paths for the given ids, in the order requested.

    Scoped to the student so one student can't combine another's bookmarks.
    Missing/foreign ids are simply dropped from the result.
    """
    stmt = select(SavedPath).where(
        SavedPath.student_id == student_id, SavedPath.id.in_(path_ids)
    )
    found = {p.id: p for p in (await session.execute(stmt)).scalars().all()}
    return [found[pid] for pid in path_ids if pid in found]


async def save_path(
    session: AsyncSession, student_id: str, alumnus_id: str, notes: str | None
) -> SavedPath:
    """Bookmark an alumnus for a student. Idempotent on (student, alumnus):
    a re-save updates the note and returns the existing row rather than erroring."""
    stmt = (
        pg_insert(SavedPath)
        .values(student_id=student_id, alumnus_id=alumnus_id, notes=notes)
        .on_conflict_do_update(
            index_elements=["student_id", "alumnus_id"], set_={"notes": notes}
        )
        .returning(SavedPath.id)
    )
    saved_id = (await session.execute(stmt)).scalar_one()
    await session.commit()
    return (
        await session.execute(select(SavedPath).where(SavedPath.id == saved_id))
    ).scalar_one()


async def delete_saved_path(session: AsyncSession, student_id: str, path_id: int) -> bool:
    path = (
        await session.execute(
            select(SavedPath).where(
                SavedPath.id == path_id, SavedPath.student_id == student_id
            )
        )
    ).scalar_one_or_none()
    if path is None:
        return False
    await session.delete(path)
    await session.commit()
    return True


async def record_run(
    session: AsyncSession,
    scope: str,
    student_id: str | None,
    alumni_scored: int,
    clusters_built: int,
    duration_ms: float,
    status: str = "ok",
    detail: str | None = None,
) -> None:
    session.add(
        PrecomputeRun(
            scope=scope,
            student_id=student_id,
            alumni_scored=alumni_scored,
            clusters_built=clusters_built,
            duration_ms=duration_ms,
            status=status,
            detail=detail,
        )
    )
    await session.commit()
