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

logging.basicConfig(level=settings.log_level)
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(settings.log_level)
    ),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
