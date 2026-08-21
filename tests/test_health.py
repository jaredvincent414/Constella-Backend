"""Readiness reports through the status code, not only the body.

A platform health check reads the code and nothing else. This endpoint used to
answer 200 with `"status": "unavailable"`, which meant a deployment that could
not reach Postgres was reported healthy and kept taking traffic.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import cache, repository
from app.api.routes import health
from app.db import get_session
from app.main import app


class _FailingSession:
    async def execute(self, *_args, **_kwargs):
        raise ConnectionError("postgres is unreachable")


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client


@pytest.fixture
def unreachable_postgres():
    app.dependency_overrides[get_session] = lambda: _FailingSession()
    yield
    app.dependency_overrides.pop(get_session, None)


async def test_liveness_never_touches_a_dependency(client):
    """`/health` answers whether the process is up, and must not fail because
    something it talks to is down — that is what readiness is for."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_is_503_when_postgres_is_unreachable(client, unreachable_postgres):
    response = await client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["postgres"] is False


async def test_readiness_still_reports_redis_when_postgres_is_down(
    client, unreachable_postgres, monkeypatch
):
    """Both are reported separately, so a 503 says which one failed."""

    async def alive():
        return True

    monkeypatch.setattr(cache, "ping", alive)
    assert (await client.get("/health/ready")).json()["redis"] is True


async def test_redis_down_alone_is_degraded_not_dead(client, monkeypatch):
    """The API recomputes on a miss and a cached read still serves, so Redis
    being down must not take the service out of the load balancer."""

    async def dead():
        return False

    async def count(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(cache, "ping", dead)
    monkeypatch.setattr(repository, "count_alumni", count)
    app.dependency_overrides[get_session] = lambda: _WorkingSession()
    try:
        response = await client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["redis"] is False
    finally:
        app.dependency_overrides.pop(get_session, None)


class _WorkingSession:
    async def execute(self, *_args, **_kwargs):
        return None


def test_the_route_sets_the_code_rather_than_raising():
    """Pinned because raising would lose the body — the diagnosis of *which*
    dependency failed is the reason this endpoint exists."""
    import inspect

    source = inspect.getsource(health.ready)
    assert "response.status_code" in source
    assert "raise HTTPException" not in source
