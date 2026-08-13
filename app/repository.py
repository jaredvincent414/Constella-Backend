"""Database access.

Kept separate from the routes so the matching engine can be exercised against
plain objects in tests without a live Postgres.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Alumnus, PrecomputeRun, Student


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
