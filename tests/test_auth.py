"""Unit tests for the token and admin-key helpers.

No database: these cover the pure functions the security boundary is built on.
The boundary itself — 401 without a token, 404 across schools — is exercised in
`test_api_security.py`, which needs Postgres and skips without it.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import cache
from app.auth import (
    _bearer,
    hash_password,
    hash_token,
    new_token,
    require_admin,
    verify_password,
)
from app.config import settings
from app.schemas import split_name


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


class TestPrincipalCacheKeying:
    """The cached principal is a credential-adjacent store. What it holds, and
    what it refuses to hold, is the whole security surface of the optimization."""

    def test_key_is_derived_from_the_token_hash_not_the_token(self):
        """A second store of plaintext tokens would be a second thing to leak.

        Keying on the hash the database already holds means a Redis dump
        discloses exactly what a database dump would, and no more.
        """
        token = "a-real-looking-token"
        key = cache.principal_key(hash_token(token))
        assert token not in key
        assert hash_token(token) in key

    def test_distinct_tokens_get_distinct_keys(self):
        assert cache.principal_key(hash_token("a")) != cache.principal_key(hash_token("b"))

    def test_key_is_cache_versioned(self):
        # A version bump has to orphan these along with everything else, or a
        # format change would be read back under the old assumptions.
        assert cache.principal_key("abc").startswith(f"constella:{cache.CACHE_VERSION}:")


class TestPrincipalCacheRefusesATenantlessCaller:
    """`school_id=None` means *unscoped* everywhere downstream. Caching one
    would be the fail-open case `current_student` exists to prevent, with a TTL
    attached — so neither side of the cache will carry it."""

    async def test_set_refuses_a_null_school(self, monkeypatch):
        written = []

        class FakeClient:
            async def set(self, *args, **kwargs):
                written.append(args)

        monkeypatch.setattr(cache, "get_client", lambda: FakeClient())
        await cache.set_principal("hash", "stu-1", None, 60)
        await cache.set_principal("hash", "stu-1", "", 60)
        await cache.set_principal("hash", "", "school-a", 60)
        assert written == []

    async def test_set_refuses_a_non_positive_ttl(self, monkeypatch):
        written = []

        class FakeClient:
            async def set(self, *args, **kwargs):
                written.append(args)

        monkeypatch.setattr(cache, "get_client", lambda: FakeClient())
        await cache.set_principal("hash", "stu-1", "school-a", 0)
        assert written == []

    async def test_get_rejects_an_entry_without_a_school(self, monkeypatch):
        class FakeClient:
            async def get(self, _key):
                return b"stu-1\x1f"

        monkeypatch.setattr(cache, "get_client", lambda: FakeClient())
        assert await cache.get_principal("hash") is None

    async def test_get_round_trips_a_valid_entry(self, monkeypatch):
        class FakeClient:
            async def get(self, _key):
                return b"stu-1\x1fschool-a"

        monkeypatch.setattr(cache, "get_client", lambda: FakeClient())
        assert await cache.get_principal("hash") == ("stu-1", "school-a")


class TestSplitName:
    """`students.name` is one column because that is all registration ever
    collected. The frontend's signup form collects two, so until the columns
    exist the split happens in the view — lossy, and deliberately visible."""

    def test_splits_a_two_part_name(self):
        assert split_name("Ada Lovelace") == ("Ada", "Lovelace")

    def test_a_single_word_has_no_last_name(self):
        assert split_name("Ada") == ("Ada", None)

    def test_extra_words_go_to_the_last_name(self):
        # Lossy and known: "Mary Jane" is a given name to some people and not
        # to others, and one column cannot tell the difference.
        assert split_name("Mary Jane Watson") == ("Mary", "Jane Watson")

    def test_blank_and_missing_names(self):
        assert split_name(None) == (None, None)
        assert split_name("") == (None, None)
        assert split_name("   ") == (None, None)

    def test_surrounding_whitespace_is_trimmed(self):
        assert split_name("  Ada  Lovelace  ") == ("Ada", "Lovelace")


class TestPasswordHashing:
    """The password is never stored, and a stored hash is never a credential."""

    def test_the_hash_does_not_contain_the_password(self):
        assert "hunter2-and-then-some" not in hash_password("hunter2-and-then-some")

    def test_verifies_the_right_password(self):
        assert verify_password("hunter2-and-then-some", hash_password("hunter2-and-then-some"))

    def test_rejects_the_wrong_password(self):
        assert not verify_password("nope", hash_password("hunter2-and-then-some"))

    def test_is_salted(self):
        """Two people with the same password must not share a hash, or one
        cracked password reveals everyone who reused it."""
        assert hash_password("same-password-here") != hash_password("same-password-here")

    def test_carries_its_algorithm_and_parameters(self):
        """So argon2id can replace scrypt later without a flag day: verify
        dispatches on the prefix and old hashes stay verifiable."""
        assert hash_password("a-password-here").startswith("scrypt$16384$8$1$")

    def test_a_missing_hash_never_verifies(self):
        """Students created before password auth have none. Such an account must
        be unable to log in, not able to log in without a password."""
        assert not verify_password("anything", None)
        assert not verify_password("anything", "")

    def test_a_malformed_hash_fails_rather_than_raising(self):
        for broken in ("garbage", "scrypt$notanumber$8$1$aa$bb", "bcrypt$1$2$3$aa$bb", "$$$$$"):
            assert not verify_password("anything", broken)

    def test_an_empty_password_never_verifies(self):
        assert not verify_password("", hash_password("a-real-password"))
