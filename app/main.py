"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import cache
from app.api.routes import (
    admin,
    alumni,
    constellation,
    dashboard,
    health,
    paths,
    search,
    simulate,
    students,
)
from app.config import settings

log = structlog.get_logger(__name__)

logging.basicConfig(level=settings.log_level)
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(settings.log_level)
    ),
)


def _dependency_host(url: str) -> str:
    """`postgresql+asyncpg://user:pw@db.example:5432/x` -> `db.example:5432`.

    Credentials are stripped rather than masked — a log line is the easiest
    place in a system to leak a password, and nothing here needs one to be
    useful.
    """
    without_scheme = url.split("://", 1)[-1]
    authority = without_scheme.split("/", 1)[0]
    return authority.rsplit("@", 1)[-1] or "unset"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Logged at startup because the alternative is discovering it by request.
    # `DATABASE_URL` unset falls back to a localhost default that is correct for
    # `docker compose` and wrong everywhere else, and the symptom — every route
    # that touches the corpus returning 500 — points at the application rather
    # than at its configuration. One line naming the host it will dial turns
    # that into something obvious from the deploy log.
    log.info(
        "starting",
        postgres=_dependency_host(settings.database_url),
        redis=_dependency_host(settings.redis_url),
        cors_origins=settings.cors_origin_list,
        admin_api="enabled" if settings.admin_api_key else "disabled",
    )
    yield
    await cache.close_client()


app = FastAPI(
    title="Constella — Cohort Matching Engine",
    description=(
        "Similarity scoring, career-outcome clustering, and the constellation "
        "payload consumed by the frontend map."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(students.router)
app.include_router(dashboard.router)
app.include_router(constellation.router)
app.include_router(alumni.router)
app.include_router(search.router)
app.include_router(simulate.router)
app.include_router(paths.router)
app.include_router(admin.router)


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "service": "constella-backend",
        "docs": "/docs",
        "health": "/health/ready",
    }
