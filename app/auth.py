"""Authentication and tenant scoping.

Two boundaries live here:

* **Students** authenticate with an opaque bearer token, issued once at
  registration and stored only as a SHA-256 hash. `current_student` resolves the
  token to the caller — every student-facing route derives the student from this
  dependency rather than trusting a `studentId` in the request, so no one can
  read another student's data by guessing an id. The student carries their
  `school_id`, which scopes all alumni access.

* **Admin** endpoints require a shared key (`ADMIN_API_KEY`). If it isn't
  configured the admin surface is *disabled*, not open — a safe default for a
  service that would otherwise expose recompute/flush to anyone.

There are two ways to resolve a caller, and the difference is what they need:

* `current_student` loads the full record, for anything that reads a profile or
  writes to one.
* `current_principal` resolves only `(student_id, school_id)` — everything the
  tenant boundary needs — and caches it, so a request served entirely from the
  constellation cache never touches Postgres at all. Auth was measured at
  ~1.6ms of a ~5ms cache hit, on top of ~0.9ms just to check out a connection.

The cached form is the reason `auth_cache_ttl_seconds` exists and is short: it
is the window in which a deleted student can still authenticate. It is not a
revocation mechanism and must not be mistaken for one — the README still lists
revocation as missing, and this does not change that.

This is the enforcement boundary, not a full identity provider: a real
deployment would have campus SSO/OIDC mint these tokens and gate registration.
Tokens assume the transport is TLS.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import cache
from app.config import settings
from app.db import SessionLocal, get_session
from app.models import Student


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_token() -> tuple[str, str]:
    """A fresh (plaintext, hash) pair. Return the plaintext to the caller once;
    persist only the hash."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


async def current_student(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Student:
    """The authenticated student, or 401. Eager-loads courses/programs so the
    scorer can build a profile without a second round trip.

    Never cached: the result is a live ORM instance that callers read profiles
    from and write profiles through. `current_principal` is the cached form, for
    callers that only need to know who is asking and which school they belong to.
    """
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return await _student_for_hash(session, hash_token(token))


@dataclass(frozen=True)
class Principal:
    """Who the caller is, and which corpus they may see. Nothing else.

    Deliberately not a `Student`: this is what gets cached, and an ORM instance
    reconstituted from a cache would be detached, silently stale, and unsafe to
    hand to code that writes.
    """

    id: str
    school_id: str


async def current_principal(
    authorization: str | None = Header(default=None),
) -> Principal:
    """The caller's identity and tenant, from cache when possible.

    Takes no session dependency on purpose. FastAPI resolves dependencies
    eagerly, so declaring one here would check out a Postgres connection for
    every request — including the ones this exists to keep off Postgres
    entirely. The miss path opens its own session instead.
    """
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token_hash = hash_token(token)

    ttl = settings.auth_cache_ttl_seconds
    if ttl > 0:
        try:
            cached = await cache.get_principal(token_hash)
        except Exception:
            cached = None  # Redis down — fall through to Postgres.
        if cached is not None:
            return Principal(id=cached[0], school_id=cached[1])

    async with SessionLocal() as session:
        student = await _student_for_hash(session, token_hash)

    if ttl > 0:
        try:
            await cache.set_principal(token_hash, student.id, student.school_id, ttl)
        except Exception:
            pass  # Caching is an optimization; never fail the request over it.

    return Principal(id=student.id, school_id=student.school_id)


async def _student_for_hash(session: AsyncSession, token_hash: str) -> Student:
    """Resolve a token hash to a student, or raise. The single place the 401/403
    rules live, so the cached and uncached paths cannot drift apart."""
    student = (
        await session.execute(
            select(Student)
            .where(Student.auth_token_hash == token_hash)
            .options(selectinload(Student.courses), selectinload(Student.programs))
        )
    ).scalar_one_or_none()
    if student is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    if student.school_id is None:
        # Fail closed. `students.school_id` is nullable only so the backfill
        # migration could land, but a null tenant would reach
        # `list_alumni(school_id=None)` — which means *unscoped*, i.e. every
        # school's corpus. An account in this state is unusable by design rather
        # than quietly over-privileged.
        raise HTTPException(status_code=403, detail="Student is not associated with a school")
    return student


async def require_admin(
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    authorization: str | None = Header(default=None),
) -> None:
    """Gate the admin surface with the configured key (constant-time compare)."""
    configured = settings.admin_api_key
    if not configured:
        raise HTTPException(status_code=503, detail="Admin API is disabled")
    presented = x_admin_key or _bearer(authorization)
    if not presented or not secrets.compare_digest(presented, configured):
        raise HTTPException(status_code=403, detail="Admin credentials required")
