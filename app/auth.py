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

This is the enforcement boundary, not a full identity provider: a real
deployment would have campus SSO/OIDC mint these tokens and gate registration.
Tokens assume the transport is TLS.
"""

from __future__ import annotations

import hashlib
import secrets

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import get_session
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
    scorer can build a profile without a second round trip."""
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    student = (
        await session.execute(
            select(Student)
            .where(Student.auth_token_hash == hash_token(token))
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
