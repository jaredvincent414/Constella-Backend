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
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import structlog

from app import cache, repository
from app.config import settings
from app.db import SessionLocal
from app.matching import StudentProfile, build_constellation_for_query

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


async def precompute_student(student_id: str, max_alumni: int | None = None) -> dict:
    """Score and cluster the corpus for one student, then cache the result."""
    started = time.perf_counter()
    limit = max_alumni or settings.constellation_max_alumni

    async with SessionLocal() as session:
        student = await repository.get_student(session, student_id)
        if student is None:
            raise LookupError(f"student {student_id!r} not found")

        # Scoped exactly as the request path scopes it. A job that scored against
        # every school would write a cross-tenant constellation into the cache,
        # and the route would serve it on the next hit without ever touching the
        # corpus itself — the isolation has to hold on both sides of Redis.
        alumni = await repository.list_alumni(session, school_id=student.school_id)
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
                profile, alumni, from_major, to_major, variant_limit
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
            alumni_scored=len(alumni),
            clusters_built=clusters_built,
            duration_ms=duration_ms,
        )

    log.info(
        "precomputed_student",
        student_id=student_id,
        alumni=len(alumni),
        queries=len(queries),
        clusters=clusters_built,
        duration_ms=round(duration_ms, 1),
    )
    return {
        "student_id": student_id,
        "alumni_scored": len(alumni),
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
        students = await repository.list_students(session)

    results = []
    for student in students:
        try:
            results.append(await precompute_student(student.id))
        except Exception as exc:  # keep going; one bad profile shouldn't stop the batch
            log.error("precompute_failed", student_id=student.id, error=str(exc))
            results.append({"student_id": student.id, "error": str(exc)})

    duration_ms = (time.perf_counter() - started) * 1000
    log.info("precomputed_all", students=len(students), duration_ms=round(duration_ms, 1))
    return {
        "students": len(students),
        "duration_ms": round(duration_ms, 1),
        "results": results,
    }


async def invalidate_student_profile(student_id: str) -> int:
    """Drop a student's cached constellations after their profile changes."""
    return await cache.invalidate_student(student_id)


if __name__ == "__main__":
    asyncio.run(precompute_all())
