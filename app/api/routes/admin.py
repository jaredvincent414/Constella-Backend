"""Operational endpoints for driving the background jobs.

These are the triggers described in the spec's data flow: re-run when new alumni
data arrives or a student's profile changes.

The whole router sits behind `require_admin`: a shared `ADMIN_API_KEY` presented
as `X-Admin-Key` (or a bearer token). With no key configured the surface answers
503 for everyone — recompute and cache-flush are expensive and destructive
enough that "unset" has to mean closed, not open. Gating at the router rather
than per-route means a new endpoint added here is protected by default.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from app import cache
from app.auth import require_admin
from app.jobs import recompute

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


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
