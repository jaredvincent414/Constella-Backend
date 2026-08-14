"""Unit tests for the token and admin-key helpers.

No database: these cover the pure functions the security boundary is built on.
The boundary itself — 401 without a token, 404 across schools — is exercised in
`test_api_security.py`, which needs Postgres and skips without it.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.auth import _bearer, hash_token, new_token, require_admin
from app.config import settings


def test_hash_token_is_deterministic_sha256():
    first = hash_token("hunter2")
    assert first == hash_token("hunter2")
    assert len(first) == 64
    assert int(first, 16) >= 0  # hex


def test_hash_token_differs_by_input():
    assert hash_token("token-a") != hash_token("token-b")


def test_new_token_returns_plaintext_and_its_hash():
    raw, hashed = new_token()
    assert raw != hashed
    assert hashed == hash_token(raw)
    # The plaintext must not be recoverable from what gets persisted.
    assert raw not in hashed


def test_new_token_is_unique_per_call():
    tokens = {new_token()[0] for _ in range(100)}
    assert len(tokens) == 100


def test_new_token_has_enough_entropy():
    # secrets.token_urlsafe(32) — 32 bytes, so ~43 urlsafe chars. A short token
    # would be guessable, and this is the only credential a student holds.
    raw, _ = new_token()
    assert len(raw) >= 40


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc123", "abc123"),
        ("bearer abc123", "abc123"),  # scheme is case-insensitive per RFC 6750
        ("BEARER abc123", "abc123"),
        ("Bearer   abc123  ", "abc123"),
        (None, None),
        ("", None),
        ("abc123", None),  # no scheme
        ("Basic abc123", None),  # wrong scheme
        ("Bearer ", None),  # scheme with no token
        ("Bearer    ", None),
    ],
)
def test_bearer_parsing(header, expected):
    assert _bearer(header) == expected


# --------------------------------------------------------------------------
# Admin gate
# --------------------------------------------------------------------------


@pytest.fixture
def admin_key(monkeypatch):
    """Configure an admin key for the duration of one test."""
    monkeypatch.setattr(settings, "admin_api_key", "s3cret-admin-key")
    return "s3cret-admin-key"


async def test_admin_disabled_when_key_unset(monkeypatch):
    """The safe default: no key configured means the surface is closed, not open."""
    monkeypatch.setattr(settings, "admin_api_key", "")
    with pytest.raises(HTTPException) as exc:
        await require_admin(x_admin_key="anything", authorization=None)
    assert exc.value.status_code == 503


async def test_admin_rejects_missing_credentials(admin_key):
    with pytest.raises(HTTPException) as exc:
        await require_admin(x_admin_key=None, authorization=None)
    assert exc.value.status_code == 403


async def test_admin_rejects_wrong_key(admin_key):
    with pytest.raises(HTTPException) as exc:
        await require_admin(x_admin_key="not-the-key", authorization=None)
    assert exc.value.status_code == 403


async def test_admin_rejects_key_prefix(admin_key):
    """A prefix of the real key must fail — compare_digest, not startswith."""
    with pytest.raises(HTTPException) as exc:
        await require_admin(x_admin_key=admin_key[:-1], authorization=None)
    assert exc.value.status_code == 403


async def test_admin_accepts_header_key(admin_key):
    assert await require_admin(x_admin_key=admin_key, authorization=None) is None


async def test_admin_accepts_bearer_key(admin_key):
    assert await require_admin(x_admin_key=None, authorization=f"Bearer {admin_key}") is None


async def test_student_token_does_not_open_admin(admin_key):
    """A valid student token is not an admin credential — separate namespaces."""
    student_token, _ = new_token()
    with pytest.raises(HTTPException) as exc:
        await require_admin(x_admin_key=None, authorization=f"Bearer {student_token}")
    assert exc.value.status_code == 403
