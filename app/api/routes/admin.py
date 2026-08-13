"""Operational endpoints for driving the background jobs.

These are the triggers described in the spec's data flow: re-run when new alumni
data arrives or a student's profile changes. Unauthenticated for local
development — put them behind auth or an internal-only route before this is
exposed anywhere real.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app import cache
from app.jobs import recompute

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/recompute")
async def recompute_all(background: BackgroundTasks, wait: bool = False) -> dict:
    """Rebuild every student's cached constellation, after an alumni import."""
    if wait:
        return await recompute.precompute_all()
    background.add_task(recompute.precompute_all)
    return {"status": "scheduled", "scope": "all"}


@router.post("/recompute/{student_id}")
async def recompute_student(student_id: str, wait: bool = False) -> dict:
    """Rebuild one student's constellation, after their profile changes."""
    if wait:
        try:
            return await recompute.precompute_student(student_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    await recompute.invalidate_student_profile(student_id)
    return {"status": "invalidated", "student_id": student_id}


@router.delete("/cache")
async def clear_cache() -> dict:
    deleted = await cache.invalidate_all_constellations()
    return {"status": "cleared", "keys_deleted": deleted}
