"""Redis cache layer.

The heavy work — scoring every alumnus against a student, then clustering the
result — runs in a background job. This module is the boundary between that job
and the request path: the job writes, the API reads. When a cache entry is
missing the API recomputes inline rather than failing, so a cold Redis degrades
latency instead of availability.
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

from app.config import settings

_client: aioredis.Redis | None = None

# Bump when the cached payload shape changes, so stale entries from an older
# deploy are ignored rather than deserialized into the wrong schema.
# v2: alumni carry a structured `programs` list (majors + minors + provenance).
CACHE_VERSION = "v2"


def get_client() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def constellation_key(student_id: str, query_hash: str) -> str:
    return f"constella:{CACHE_VERSION}:constellation:{student_id}:{query_hash}"


def timeline_key(alumnus_id: str) -> str:
    return f"constella:{CACHE_VERSION}:timeline:{alumnus_id}"


def student_index_key(student_id: str) -> str:
    """Set of every cache key derived from this student, for targeted invalidation."""
    return f"constella:{CACHE_VERSION}:index:student:{student_id}"


async def get_json(key: str) -> Any | None:
    raw = await get_client().get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # A corrupt entry should behave exactly like a miss.
        await get_client().delete(key)
        return None


async def set_json(key: str, value: Any, ttl: int | None = None) -> None:
    await get_client().set(
        key,
        json.dumps(value, separators=(",", ":")),
        ex=ttl if ttl is not None else settings.cache_ttl_seconds,
    )


async def track_student_key(student_id: str, key: str) -> None:
    """Record that `key` belongs to `student_id` so it can be invalidated later."""
    client = get_client()
    index = student_index_key(student_id)
    await client.sadd(index, key)
    # The index must outlive the entries it points at, or invalidation misses them.
    await client.expire(index, settings.cache_ttl_seconds * 2)


async def invalidate_student(student_id: str) -> int:
    """Drop every cached constellation for a student.

    Called when their profile changes — new courses, a new semester, a new
    pivot query — since all of those change the scores.
    """
    client = get_client()
    index = student_index_key(student_id)
    keys = await client.smembers(index)
    if not keys:
        return 0
    deleted = await client.delete(*keys)
    await client.delete(index)
    return deleted


async def invalidate_all_constellations() -> int:
    """Drop every cached constellation.

    Called when the alumni corpus changes, which invalidates every student's
    scores at once. SCAN rather than KEYS so this stays safe on a live server.
    """
    client = get_client()
    pattern = f"constella:{CACHE_VERSION}:constellation:*"
    deleted = 0
    async for key in client.scan_iter(match=pattern, count=500):
        deleted += await client.delete(key)
    index_pattern = f"constella:{CACHE_VERSION}:index:student:*"
    async for key in client.scan_iter(match=index_pattern, count=500):
        await client.delete(key)
    return deleted


async def ping() -> bool:
    try:
        return bool(await get_client().ping())
    except Exception:
        return False
