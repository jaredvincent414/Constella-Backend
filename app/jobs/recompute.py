"""Background precompute jobs.

The spec's data flow puts the heavy work here rather than on the request path:

    Postgres -> background job (scores + clusters) -> Redis -> FastAPI

Jobs re-run when new alumni data lands or a student's profile changes — not on
every page load. This module holds the job bodies; `app.api.routes.admin`
exposes them as endpoints, and they're runnable standalone via
`python -m app.jobs.recompute`.
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import structlog

from app import cache, repository
from app.config import settings
from app.db import SessionLocal
from app.matching import StudentProfile, build_constellation

log = structlog.get_logger(__name__)


def query_hash(from_major: str | None, to_major: str | None, max_alumni: int) -> str:
    """Cache-key component covering everything that changes the result.

    A student's constellation differs per pivot query, so the query has to be
    part of the key or a What If run would poison the broad-explore cache.
    """
    raw = f"{from_major or ''}|{to_major or ''}|{max_alumni}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def precompute_student(student_id: str, max_alumni: int | None = None) -> dict:
    """Score and cluster the corpus for one student, then cache the result."""
    started = time.perf_counter()
    limit = max_alumni or settings.constellation_max_alumni

    async with SessionLocal() as session:
        student = await repository.get_student(session, student_id)
        if student is None:
            raise LookupError(f"student {student_id!r} not found")

        alumni = await repository.list_alumni(session)
        profile = StudentProfile.from_model(student)
        response, _ = build_constellation(profile, alumni, max_alumni=limit)

        duration_ms = (time.perf_counter() - started) * 1000

        key = cache.constellation_key(student_id, query_hash(None, None, limit))
        await cache.set_json(key, response.model_dump(by_alias=True))
        await cache.track_student_key(student_id, key)

        await repository.record_run(
            session,
            scope="student",
            student_id=student_id,
            alumni_scored=len(alumni),
            clusters_built=len(response.clusters),
            duration_ms=duration_ms,
        )

    log.info(
        "precomputed_student",
        student_id=student_id,
        alumni=len(alumni),
        clusters=len(response.clusters),
        duration_ms=round(duration_ms, 1),
    )
    return {
        "student_id": student_id,
        "alumni_scored": len(alumni),
        "clusters": len(response.clusters),
        "duration_ms": round(duration_ms, 1),
    }


async def precompute_all() -> dict:
    """Rebuild every student's constellation.

    This is the job to run after an alumni import: new alumni change every
    student's scores, so per-student invalidation isn't enough.
    """
    started = time.perf_counter()
    await cache.invalidate_all_constellations()

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
