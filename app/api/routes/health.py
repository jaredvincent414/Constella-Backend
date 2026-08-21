"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, repository
from app.db import get_session

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(response: Response, session: AsyncSession = Depends(get_session)) -> dict:
    """Reports both dependencies separately, and answers with a status code.

    Redis being down is degraded, not dead — the API recomputes on a miss, and
    a cached read still serves — so that stays a 200 with `redis: false`.
    Postgres being down means no request that touches the corpus can be served,
    so it is a 503.

    The status code is the point. A load balancer or platform health check reads
    that and nothing else; this endpoint previously returned 200 with
    `"status": "unavailable"` in the body, which meant a deployment with no
    database reachable was reported healthy and kept taking traffic.
    """
    try:
        await session.execute(select(1))
        db_ok = True
    except Exception:
        db_ok = False

    redis_ok = await cache.ping()

    alumni = await repository.count_alumni(session) if db_ok else 0

    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if db_ok else "unavailable",
        "postgres": db_ok,
        "redis": redis_ok,
        "degraded": db_ok and not redis_ok,
        "alumni_count": alumni,
    }
