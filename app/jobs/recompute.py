"""Background precompute jobs.

The spec's data flow puts the heavy work here rather than on the request path:

    Postgres -> background job (scores + clusters) -> Redis -> FastAPI

Jobs re-run when new alumni data lands or a student's profile changes — not on
every page load. This module holds the job bodies; `app.api.routes.admin`
exposes them as endpoints, and they're runnable standalone via
`python -m app.jobs.recompute`.

Two things the job is careful about, both of which cost nothing to get right and
are expensive to get wrong:

* **It overwrites in place and clears stale entries afterwards.** Flushing the
  cache up front and rebuilding into the hole means every student is cold from
  the moment the job starts until their turn comes — self-inflicted, and worst
  on exactly the corpus sizes that make the job slow in the first place.

* **It re-warms the queries students actually ran**, not only the bare explore.
  A student whose page always sends a pivot query would otherwise miss the cache
  every single time while the job dutifully warmed an entry nothing requests.

* **It loads each school's corpus once**, not once per student. Roughly 90% of
  the old per-student cost was the corpus load and the ORM hydration behind it,
  for a value identical across every student at that school.
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, repository
from app.config import settings
from app.db import SessionLocal
from app.matching import Corpus, StudentProfile, build_constellation_for_query, build_corpus
from app.models import Student

log = structlog.get_logger(__name__)

# (from_major, to_major, max_alumni) — the three inputs the cache key covers.
Query = tuple[str | None, str | None, int]


def query_hash(from_major: str | None, to_major: str | None, max_alumni: int) -> str:
    """Cache-key component covering everything that changes the result.

    A student's constellation differs per pivot query, so the query has to be
    part of the key or a What If run would poison the broad-explore cache.
    """
    raw = f"{from_major or ''}|{to_major or ''}|{max_alumni}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def queries_to_warm(student_id: str, limit: int) -> list[Query]:
    """The bare explore query, plus whatever else this student has been asking.

    Read off the student's cache index, which records the query behind every
    entry. Demand is the only honest source for this list — the alternative is
    guessing at popular pivots, which warms entries nobody opens.

    Capped, because the index grows with user requests: an unbounded list would
    let one student's exploration set the runtime of the whole job.
    """
    base: Query = (None, None, limit)
    queries: list[Query] = [base]
    try:
        entries = await cache.student_index_entries(student_id)
    except Exception:
        # A cache the job can't read is a cache with nothing worth re-warming.
        return queries

    for params in entries.values():
        if params.get("kind") != cache.KIND_CONSTELLATION:
            continue
        try:
            variant: Query = (
                params.get("fromMajor"),
                params.get("toMajor"),
                int(params.get("maxAlumni") or limit),
            )
        except (TypeError, ValueError):
            continue
        if variant not in queries:
            queries.append(variant)
        if len(queries) >= settings.precompute_max_queries_per_student:
            break
    return queries


async def precompute_student(
    student_id: str, max_alumni: int | None = None, corpus: Corpus | None = None
) -> dict:
    """Score and cluster the corpus for one student, then cache the result.

    `corpus` lets the batch job hand in a corpus it already built for this
    student's school. Left out, one is loaded — which is what the admin
    single-student endpoint wants.
    """
    async with SessionLocal() as session:
        student = await repository.get_student(session, student_id)
        if student is None:
            raise LookupError(f"student {student_id!r} not found")
        if corpus is None:
            corpus = await load_corpus(session, student.school_id)
        return await warm_student(session, student, corpus, max_alumni)


async def load_corpus(session: AsyncSession, school_id: str | None) -> Corpus:
    """One school's alumni, prepared for scoring.

    Refuses a null tenant. `list_alumni(school_id=None)` means *unscoped*, so
    building a corpus for a tenantless student would hand them every school's
    records — the same fail-open `current_student` exists to prevent on the
    request path.
    """
    if school_id is None:
        raise ValueError("refusing to build an unscoped corpus")
    alumni = await repository.list_alumni(session, school_id=school_id)
    return build_corpus(alumni, school_id=school_id)


async def warm_student(
    session: AsyncSession,
    student: Student,
    corpus: Corpus,
    max_alumni: int | None = None,
) -> dict:
    """Warm one student's cache entries against an already-prepared corpus."""
    started = time.perf_counter()
    limit = max_alumni or settings.constellation_max_alumni
    student_id = student.id

    # Scoped exactly as the request path scopes it. A job that scored against
    # every school would write a cross-tenant constellation into the cache, and
    # the route would serve it on the next hit without ever touching the corpus
    # itself — the isolation has to hold on both sides of Redis. Now that the
    # corpus is built once and passed around, this is the assertion that the
    # batching didn't quietly hand a student the wrong school.
    if corpus.school_id != student.school_id:
        raise ValueError(
            f"corpus is for school {corpus.school_id!r}, "
            f"student {student_id!r} belongs to {student.school_id!r}"
        )

    profile = StudentProfile.from_model(student)
    queries = await queries_to_warm(student_id, limit)
    written: set[str] = set()
    clusters_built = 0

    for from_major, to_major, variant_limit in queries:
        key = cache.constellation_key(
            student_id, query_hash(from_major, to_major, variant_limit)
        )
        # Same function the route calls, so a warmed entry is byte-identical
        # to what the request path would have produced for that key.
        response = build_constellation_for_query(
            profile, corpus, from_major, to_major, variant_limit
        )
        await cache.set_raw(key, cache.serialize_cached(response.model_dump(by_alias=True)))
        await cache.track_student_key(
            student_id,
            key,
            {
                "kind": cache.KIND_CONSTELLATION,
                "fromMajor": from_major,
                "toMajor": to_major,
                "maxAlumni": variant_limit,
            },
        )
        written.add(key)
        if (from_major, to_major, variant_limit) == (None, None, limit):
            clusters_built = len(response.clusters)

    # Everything else this student had cached is now stale — timelines and
    # combined paths included, since both are derived from the corpus that
    # just changed. Sparing what we just wrote is what keeps the rebuild
    # from opening a window where the constellation is missing.
    await cache.invalidate_student(student_id, keep=written)

    duration_ms = (time.perf_counter() - started) * 1000

    await repository.record_run(
        session,
        scope="student",
        student_id=student_id,
        alumni_scored=len(corpus),
        clusters_built=clusters_built,
        duration_ms=duration_ms,
    )

    log.info(
        "precomputed_student",
        student_id=student_id,
        alumni=len(corpus),
        queries=len(queries),
        clusters=clusters_built,
        duration_ms=round(duration_ms, 1),
    )
    return {
        "student_id": student_id,
        "alumni_scored": len(corpus),
        "queries_warmed": len(queries),
        "clusters": clusters_built,
        "duration_ms": round(duration_ms, 1),
    }


async def precompute_all() -> dict:
    """Rebuild every student's constellation.

    This is the job to run after an alumni import: new alumni change every
    student's scores, so per-student invalidation isn't enough.

    Note what this does *not* do: flush the cache first. Every student here gets
    overwritten in place and has their stale entries dropped afterwards, so a
    global flush would buy nothing except a cache-wide cold window lasting the
    length of the run. `/api/admin/cache` is still there for a deliberate flush.
    """
    started = time.perf_counter()

    async with SessionLocal() as session:
        school_ids = await repository.list_student_school_ids(session)

    results: list[dict] = []
    students_seen = 0
    for school_id in school_ids:
        if school_id is None:
            # Tenantless students can't authenticate (`current_student` 403s), and
            # a corpus for them would be unscoped. Skip loudly rather than build
            # one.
            log.warning("precompute_skipped_tenantless_students")
            continue

        # One session and one corpus per school. The corpus is the same for every
        # student at that school, and it used to be loaded and hydrated once per
        # student — the dominant cost of the whole job.
        async with SessionLocal() as session:
            corpus = await load_corpus(session, school_id)
            students = await repository.list_students(session, school_id=school_id)
            students_seen += len(students)
            log.info("precompute_school", school_id=school_id, students=len(students),
                     alumni=len(corpus))

            for student in students:
                try:
                    results.append(await warm_student(session, student, corpus))
                except Exception as exc:  # one bad profile shouldn't stop the batch
                    log.error("precompute_failed", student_id=student.id, error=str(exc))
                    results.append({"student_id": student.id, "error": str(exc)})

    duration_ms = (time.perf_counter() - started) * 1000
    log.info("precomputed_all", students=students_seen, duration_ms=round(duration_ms, 1))
    return {
        "students": students_seen,
        "schools": len([s for s in school_ids if s is not None]),
        "duration_ms": round(duration_ms, 1),
        "results": results,
    }


async def invalidate_student_profile(student_id: str) -> int:
    """Drop a student's cached constellations after their profile changes."""
    return await cache.invalidate_student(student_id)


if __name__ == "__main__":
    asyncio.run(precompute_all())
