"""Database access.

Kept separate from the routes so the matching engine can be exercised against
plain objects in tests without a live Postgres.
"""

from __future__ import annotations

import re

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    Alumnus,
    PrecomputeRun,
    ProgramRole,
    SavedPath,
    School,
    Student,
    StudentCourse,
    StudentProgram,
    StudentYear,
)


async def get_student(session: AsyncSession, student_id: str) -> Student | None:
    """One student with courses and programs loaded.

    `populate_existing` makes this a genuine re-read: sessions are configured
    with `expire_on_commit=False`, so an instance already in the identity map
    would otherwise keep the collections it had *before* a write, and the
    profile/course update endpoints would echo back the old transcript.
    """
    stmt = (
        select(Student)
        .where(Student.id == student_id)
        .options(selectinload(Student.courses), selectinload(Student.programs))
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# Where rows land when a source doesn't name an institution. Matches the slug
# the schools/auth migration backfills orphans to, so a re-load doesn't strand
# pre-migration rows in a school nothing else references.
DEFAULT_SCHOOL_ID = "demo-university"
DEFAULT_SCHOOL_NAME = "Demo University"


def slugify_school(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "school"


async def get_or_create_school(session: AsyncSession, name: str) -> School:
    slug = slugify_school(name)
    school = await session.get(School, slug)
    if school is None:
        school = School(id=slug, name=name)
        session.add(school)
        await session.flush()
    return school


async def get_school(session: AsyncSession, slug: str) -> School | None:
    return await session.get(School, slug)


async def list_schools(session: AsyncSession) -> list[School]:
    """The schools a student can register under. Public by design — a signup
    form has to name them — so it carries nothing but slug and display name."""
    return list((await session.execute(select(School).order_by(School.name))).scalars().all())


async def get_student_by_email(session: AsyncSession, email: str) -> Student | None:
    stmt = select(Student).where(Student.email == email)
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_student(
    session: AsyncSession,
    *,
    student_id: str,
    school_id: str,
    name: str | None,
    email: str | None,
    year: StudentYear,
    auth_token_hash: str,
) -> Student:
    session.add(
        Student(
            id=student_id,
            school_id=school_id,
            name=name,
            email=email,
            year=year,
            auth_token_hash=auth_token_hash,
            interests=[],
        )
    )
    await session.commit()
    student = await get_student(session, student_id)
    assert student is not None
    return student


async def update_student_profile(
    session: AsyncSession, student: Student, updates: dict
) -> Student:
    """Apply a partial profile update.

    `updates` holds only the keys the caller actually sent (FastAPI's
    `exclude_unset`), so an omitted field keeps its value while an explicit
    `null` clears it — the two cases a PUT has to tell apart.

    A `major` change rewrites the student's `role='primary'` program row rather
    than the deprecated `declared_major` scalar, since `student_majors()` reads
    the join table. Second majors and minors are left alone: this endpoint owns
    the primary declaration only.
    """
    if "year" in updates:
        student.year = updates["year"]
    if "intended_direction" in updates:
        student.intended_direction = updates["intended_direction"]
    if "interests" in updates:
        student.interests = list(updates["interests"] or [])

    if "major" in updates:
        major = updates["major"]
        await session.execute(
            delete(StudentProgram).where(
                StudentProgram.student_id == student.id,
                StudentProgram.role == ProgramRole.primary,
            )
        )
        if major:
            session.add(
                StudentProgram(
                    student_id=student.id, name=major, role=ProgramRole.primary, term=0
                )
            )
        # Kept in sync while the deprecated column exists, so the mirror can't
        # drift from the join table it's supposed to mirror.
        student.declared_major = major

    await session.commit()
    refreshed = await get_student(session, student.id)
    assert refreshed is not None
    return refreshed


async def replace_student_courses(
    session: AsyncSession, student: Student, courses: list[tuple[str, str, int]]
) -> Student:
    """Replace the student's transcript with `(code, name, semester_index)` rows.

    Replace rather than merge: the client sends the transcript it believes in,
    and a merge would make a removed course impossible to express. Duplicate
    codes collapse to the first occurrence — the table's unique index on
    (student, code) would otherwise reject the whole write.
    """
    await session.execute(
        delete(StudentCourse).where(StudentCourse.student_id == student.id)
    )
    seen: set[str] = set()
    for code, name, semester_index in courses:
        if code in seen:
            continue
        seen.add(code)
        session.add(
            StudentCourse(
                student_id=student.id,
                course_code=code,
                course_name=name,
                semester_index=semester_index,
            )
        )
    await session.commit()
    refreshed = await get_student(session, student.id)
    assert refreshed is not None
    return refreshed


async def list_students(session: AsyncSession, school_id: str | None = None) -> list[Student]:
    """Students, optionally restricted to one school.

    Programs are eager-loaded alongside courses because `StudentProfile.from_model`
    reads both. Without them the precompute job had to re-fetch every student it
    had just listed.
    """
    stmt = (
        select(Student)
        .options(selectinload(Student.courses), selectinload(Student.programs))
        .order_by(Student.id)
    )
    if school_id is not None:
        stmt = stmt.where(Student.school_id == school_id)
    return list((await session.execute(stmt)).scalars().unique().all())


async def list_student_school_ids(session: AsyncSession) -> list[str | None]:
    """The distinct schools that have students, for batching the precompute job.

    Returned including a possible `None`, deliberately — the caller has to decide
    what to do with tenantless students rather than have them silently folded in
    with someone else's.
    """
    stmt = select(Student.school_id).distinct().order_by(Student.school_id)
    return list((await session.execute(stmt)).scalars().all())


def _alumni_query():
    return select(Alumnus).options(
        selectinload(Alumnus.courses),
        selectinload(Alumnus.majors),
        selectinload(Alumnus.pivots),
        selectinload(Alumnus.milestones),
        selectinload(Alumnus.outcomes),
    )


async def list_alumni(session: AsyncSession, school_id: str | None = None) -> list[Alumnus]:
    """Load the corpus with relationships eager-loaded, scoped to a school.

    The scorer touches every alumnus's courses, majors, and pivots, so lazy
    loading here would mean thousands of round trips per request. Passing
    `school_id` restricts the corpus to one tenant — a student never scores
    against another school's alumni.

    `school_id=None` means *unscoped*, and is for offline jobs that legitimately
    walk the whole corpus. On a request path that default is fail-open, which is
    why `current_student` refuses to authenticate a student without a school
    rather than letting a null tenant arrive here.
    """
    stmt = _alumni_query().order_by(Alumnus.id)
    if school_id is not None:
        stmt = stmt.where(Alumnus.school_id == school_id)
    return list((await session.execute(stmt)).scalars().unique().all())


async def get_alumnus(
    session: AsyncSession, alumnus_id: str, school_id: str | None = None
) -> Alumnus | None:
    """One alumnus, optionally required to be in `school_id`. A cross-school id
    returns None so the route can 404 without confirming the record exists."""
    stmt = _alumni_query().where(Alumnus.id == alumnus_id)
    if school_id is not None:
        stmt = stmt.where(Alumnus.school_id == school_id)
    return (await session.execute(stmt)).scalars().unique().one_or_none()


async def list_alumni_by_ids(
    session: AsyncSession, alumnus_ids: list[str], school_id: str | None = None
) -> list[Alumnus]:
    """Several alumni in one query, in the order requested.

    The saved-paths and combine routes hold a list of ids and need the full
    object for each. Fetching them one at a time is six round trips per id once
    the eager loads are counted — an `IN` costs the same six for the whole list.

    Scoped like `get_alumnus`: an id outside `school_id` is simply absent from
    the result, so the caller 404s without confirming the record exists.
    """
    if not alumnus_ids:
        return []
    stmt = _alumni_query().where(Alumnus.id.in_(set(alumnus_ids)))
    if school_id is not None:
        stmt = stmt.where(Alumnus.school_id == school_id)
    found = {a.id: a for a in (await session.execute(stmt)).scalars().unique().all()}
    return [found[aid] for aid in alumnus_ids if aid in found]


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
