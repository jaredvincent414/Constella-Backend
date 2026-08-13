"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, repository
from app.db import get_session

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(session: AsyncSession = Depends(get_session)) -> dict:
    """Reports both dependencies separately.

    Redis being down is degraded, not dead — the API recomputes on a miss — so
    this distinguishes the two rather than collapsing them into one boolean.
    """
    try:
        await session.execute(select(1))
        db_ok = True
    except Exception:
        db_ok = False

    redis_ok = await cache.ping()

    alumni = await repository.count_alumni(session) if db_ok else 0

    return {
        "status": "ok" if db_ok else "unavailable",
        "postgres": db_ok,
        "redis": redis_ok,
        "degraded": db_ok and not redis_ok,
        "alumni_count": alumni,
    }
