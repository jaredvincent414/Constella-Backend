"""Database access.

Kept separate from the routes so the matching engine can be exercised against
plain objects in tests without a live Postgres.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, func, select, true
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import (
    MAJOR_ROLES,
    ActivityKind,
    Alumnus,
    AlumnusCourse,
    AlumnusMajor,
    CareerOutcome,
    PrecomputeRun,
    ProgramRole,
    Provenance,
    SavedPath,
    School,
    Student,
    StudentActivity,
    StudentCourse,
    StudentProgram,
    StudentYear,
)
from app.search import SearchHit, cluster_hit, like_pattern, major_hit

log = structlog.get_logger(__name__)


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
    return (await session.execute(select(func.count(Alumnus.id)))).scalar_one()


# --------------------------------------------------------------------------
# Search — GET /api/search
# --------------------------------------------------------------------------
#
# Matching is `ILIKE '%q%'` ranked by pg_trgm's `similarity()`. Substring rather
# than similarity alone because this is a typeahead: "bioch" has to reach
# "Biochemistry", and a similarity threshold low enough to admit a five-character
# prefix of a twelve-character name admits most of the corpus with it. The GIN
# trigram indexes (`ix_alumnus_majors_name_trgm`, `ix_alumni_career_area_trgm`,
# `ix_alumnus_courses_code_trgm`) serve the ILIKE; similarity() only orders what
# survives it.


def _require_school(school_id: str | None) -> str:
    """Search has no offline caller, so it has no legitimate unscoped form.

    `school_id=None` means *every school* to `list_alumni` — the fail-open
    default the auth layer exists to keep off the request path. Here it is
    simply a bug, and one that would leak another school's majors, clusters, and
    course codes into a dropdown, so it raises rather than defaulting.
    """
    if not school_id:
        raise ValueError("search must be scoped to a school")
    return school_id


async def search_majors(
    session: AsyncSession, query: str, *, school_id: str, limit: int
) -> list[SearchHit]:
    """Majors held by this school's alumni, with how many hold each.

    Restricted to the major roles: `alumnus_majors` also holds minors, and the
    inferred ones are `provenance='derived'`. Offering an inferred minor as a
    "major" would assert something the data doesn't say, and Explore's `?major=`
    facet — the thing a row like this is for — matches majors either way.
    """
    scope = _require_school(school_id)
    score = func.max(func.similarity(AlumnusMajor.name, query)).label("score")
    holders = func.count(func.distinct(AlumnusMajor.alumnus_id)).label("holders")
    stmt = (
        select(AlumnusMajor.name, holders, score)
        .join(Alumnus, Alumnus.id == AlumnusMajor.alumnus_id)
        .where(
            Alumnus.school_id == scope,
            AlumnusMajor.role.in_(sorted(MAJOR_ROLES)),
            AlumnusMajor.name.ilike(like_pattern(query)),
        )
        .group_by(AlumnusMajor.name)
        .order_by(score.desc(), holders.desc(), AlumnusMajor.name)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [major_hit(name, count, float(value)) for name, count, value in rows]


async def search_career_clusters(
    session: AsyncSession, query: str, *, school_id: str, limit: int
) -> list[SearchHit]:
    """Career clusters, matched on the label the constellation actually draws.

    That label is `outcome_industry`: the alumnus's latest career outcome if
    they have one, else the academic `career_area` stand-in. Resolving it here —
    a lateral pick of the latest snapshot, mirroring `Alumnus.primary_outcome` —
    is what makes the returned id equal to the cluster id on the map. Searching
    `career_area` alone would use the trigram index but miss every cluster on a
    corpus that has employment data, since that is exactly the case where the
    two columns disagree.

    The cost is that this one query filters on a computed label and so cannot
    ride an index. It is bounded by the school's corpus rather than the whole
    table, which is the same bound every other read on this path already has.
    """
    scope = _require_school(school_id)
    latest = (
        select(CareerOutcome.industry, CareerOutcome.provenance)
        .where(CareerOutcome.alumnus_id == Alumnus.id)
        .order_by(CareerOutcome.years_post_grad.desc(), CareerOutcome.id.desc())
        .limit(1)
        .lateral("latest_outcome")
    )
    label = func.coalesce(latest.c.industry, Alumnus.career_area)
    members = func.count().label("members")
    score = func.max(func.similarity(label, query)).label("score")
    # Conservative on purpose: one synthetic member is enough to make the label
    # not-reported, and under-labeling provenance is the failure that matters.
    synthetic = func.bool_or(latest.c.provenance == Provenance.synthetic).label("synthetic")

    stmt = (
        select(label.label("label"), members, score, synthetic)
        .select_from(Alumnus)
        .outerjoin(latest, true())
        .where(Alumnus.school_id == scope, label.ilike(like_pattern(query)))
        .group_by(label)
        .order_by(score.desc(), members.desc(), label)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        cluster_hit(text, count, float(value), Provenance.synthetic.value if flag else None)
        for text, count, value, flag in rows
    ]


async def search_alumni_by_course(
    session: AsyncSession, query: str, *, school_id: str, limit: int
) -> list[tuple[Alumnus, float]]:
    """Alumni who took a course whose code matches, best match first.

    Two steps rather than one join: the aggregate picks the ids and their score,
    then the full objects come back through `list_alumni_by_ids`, which is
    eager-loaded (the caller needs majors and courses to render a row) and
    school-scoped a second time. The scope is already enforced above; repeating
    it costs nothing and means no future edit to this query can widen it.
    """
    scope = _require_school(school_id)
    score = func.max(func.similarity(AlumnusCourse.course_code, query)).label("score")
    stmt = (
        select(AlumnusCourse.alumnus_id, score)
        .join(Alumnus, Alumnus.id == AlumnusCourse.alumnus_id)
        .where(
            Alumnus.school_id == scope,
            AlumnusCourse.course_code.ilike(like_pattern(query)),
        )
        .group_by(AlumnusCourse.alumnus_id)
        # Ties are the norm — everyone who took the course scores identically —
        # so the id breaks them, or the same query pages differently each call.
        .order_by(score.desc(), AlumnusCourse.alumnus_id)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    scores = {alumnus_id: float(value) for alumnus_id, value in rows}
    alumni = await list_alumni_by_ids(session, list(scores), school_id=scope)
    return [(alumnus, scores[alumnus.id]) for alumnus in alumni]


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


async def record_activity(
    session: AsyncSession, student_id: str, kind: ActivityKind, label: str
) -> None:
    """Append to a student's feed, unless it would repeat the last entry.

    Without the guard, toggling a filter three times leaves three identical
    lines in a four-item feed. The dedupe window is short and only looks at the
    newest entry: the point is to collapse a burst, not to hide that someone
    came back to the same cluster tomorrow.

    Never raises. The feed is a nicety attached to actions that have already
    happened — failing a save because its breadcrumb didn't write would be the
    wrong trade.
    """
    try:
        cutoff = datetime.now(UTC) - timedelta(seconds=settings.activity_dedupe_seconds)
        latest = (
            await session.execute(
                select(StudentActivity)
                .where(StudentActivity.student_id == student_id)
                .order_by(StudentActivity.created_at.desc(), StudentActivity.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if (
            latest is not None
            and latest.kind == kind
            and latest.label == label
            and latest.created_at >= cutoff
        ):
            return

        session.add(StudentActivity(student_id=student_id, kind=kind, label=label))
        await session.commit()
    except Exception:
        await session.rollback()
        log.warning("activity_write_failed", student_id=student_id, kind=str(kind))


async def list_activity(
    session: AsyncSession, student_id: str, limit: int
) -> list[StudentActivity]:
    """The student's newest entries. Answered from ix_student_activity_recent."""
    stmt = (
        select(StudentActivity)
        .where(StudentActivity.student_id == student_id)
        .order_by(StudentActivity.created_at.desc(), StudentActivity.id.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


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
