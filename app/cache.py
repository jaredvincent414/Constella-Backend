"""Redis cache layer.

The heavy work — scoring every alumnus against a student, then clustering the
result — runs in a background job. This module is the boundary between that job
and the request path: the job writes, the API reads. When a cache entry is
missing the API recomputes inline rather than failing, so a cold Redis degrades
latency instead of availability.

Two properties this module exists to protect:

* **A hit costs one GET and nothing else.** Entries are stored as the exact
  bytes the API returns, so a hit is `GET` -> write to socket. Parsing the JSON
  back into a model only to have FastAPI re-serialize it would spend more time
  on a hit than the round trip it saved.

* **Entries are stored gzipped, and served that way.** A constellation is ~97%
  a repetitive array of alumni records, which gzip takes from 78.8 KB to 4.1 KB
  — a 19x smaller working set, and the same 19x off the wire, since the browser
  accepts `Content-Encoding: gzip` and the compressed bytes go out untouched.
  Compression happens on the write path, which runs once a day per entry; the
  read path never decompresses unless a client says it can't take gzip.

  The rejected alternative was splitting the payload into a per-school alumnus
  dictionary plus a thin per-student list. 75% of a constellation is
  student-independent, so that saves ~4x — but it costs a second round trip and
  a join on every hit, which is the property this module exists to protect.

* **Entries know how to rebuild themselves.** The per-student index records the
  query behind each key, not just the key, so the nightly job can re-warm the
  queries a student actually ran instead of only the bare one.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json

import redis.asyncio as aioredis

from app.config import settings

_client: aioredis.Redis | None = None

# Bump when the cached payload shape changes, so stale entries from an older
# deploy are ignored rather than deserialized into the wrong schema.
# v2: alumni carry a structured `programs` list (majors + minors + provenance).
# v3: clusters key on career outcome (industry); outcomes carry employment data.
# v4: program identity keys on name, so major_match is no longer a constant
#     against CIP-carrying sources. Same shape, different scores — cached
#     entries would otherwise pin every student to the old flat ranking.
# v5: alumni carry a `matchReason` (nodes) / structured match reason (detail).
# v6: the payload is unchanged, but the per-student index changed type — a SET
#     of keys became a HASH of key -> the query that produced it. The index key
#     is versioned too, so bumping is what keeps a v5 SET from colliding with a
#     v6 HGETALL and raising WRONGTYPE against a live Redis.
# v7: entries are gzipped bytes rather than JSON text. A v6 entry read as v7
#     would fail the magic-number check and be discarded as corrupt, which is
#     survivable — but the version is what makes it a clean miss instead.
# v8: the constellation payload nests alumni inside their cluster, scores them
#     0..100, and carries totalAlumni/summary. A v7 entry deserialized as v8
#     would be missing `clusters[].alumni` entirely.
CACHE_VERSION = "v8"

# Level 6 is the knee: level 1 gets 13.8x, 6 gets 19.2x, and 9 gets 20.2x for
# more CPU than the extra 5% is worth. All three cost well under a millisecond
# on a payload this size, and only the write path pays it.
GZIP_LEVEL = 6

# gzip's magic number. A stored value not starting with these bytes is truncated
# or left over from an older format, and must behave exactly like a miss.
_GZIP_MAGIC = b"\x1f\x8b"

# Index entry kinds. Only `constellation` carries enough to rebuild; the others
# are recorded so invalidation can find them, and the nightly job drops them.
KIND_CONSTELLATION = "constellation"
KIND_TIMELINE = "timeline"
KIND_COMBINE = "combine"


def get_client() -> aioredis.Redis:
    global _client
    if _client is None:
        # Bytes, not str: cached payloads are gzip, and a client that decodes
        # responses would mangle them. The few text reads (the student index)
        # decode explicitly, which is honest about where the boundary is.
        _client = aioredis.from_url(settings.redis_url, decode_responses=False)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def constellation_key(student_id: str, query_hash: str) -> str:
    return f"constella:{CACHE_VERSION}:constellation:{student_id}:{query_hash}"


def timeline_key(student_id: str, alumnus_id: str) -> str:
    """Keyed by student *and* alumnus, never by alumnus alone.

    The detail payload is a comparison: `shared_courses` is rendered with the
    viewing student's own course names, and the score breakdown is theirs. An
    alumnus-only key would serve one student's transcript overlap to the next
    student who opened the same node.
    """
    return f"constella:{CACHE_VERSION}:timeline:{student_id}:{alumnus_id}"


def combined_path_key(student_id: str, alumnus_ids: list[str]) -> str:
    """Keyed by the student and the *sorted set* of alumni combined, so the same
    combination always hits the same entry regardless of selection order."""
    digest = hashlib.sha256("|".join(sorted(set(alumnus_ids))).encode()).hexdigest()[:16]
    return f"constella:{CACHE_VERSION}:combine:{student_id}:{digest}"


def student_index_key(student_id: str) -> str:
    """Hash of every cache key derived from this student -> the query behind it.

    Targeted invalidation needs the keys; nightly re-warming needs the queries.
    """
    return f"constella:{CACHE_VERSION}:index:student:{student_id}"


# --------------------------------------------------------------------------
# Reads and writes
# --------------------------------------------------------------------------


def serialize_cached(payload: dict) -> bytes:
    """Serialize a response payload as the *cache's* copy of it.

    `meta.cached` is stamped true at write time rather than patched on read.
    The stored blob is by definition the one that will be served from cache, and
    stamping it here is what lets a hit return the bytes untouched — the reason
    the read path can skip parsing and revalidation entirely.
    """
    meta = payload.get("meta")
    if isinstance(meta, dict):
        meta["cached"] = True
    # `ensure_ascii=False` to match what FastAPI would have emitted for the same
    # payload. Escaping non-ASCII would still parse identically, but it makes the
    # cached copy differ byte-for-byte from the computed one — and every em dash
    # in a match reason would cost six bytes instead of three.
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    # mtime=0 so the same payload always produces the same bytes; gzip otherwise
    # stamps the header with the current time and every write looks like a change.
    return gzip.compress(text.encode(), compresslevel=GZIP_LEVEL, mtime=0)


def decompress(blob: bytes) -> bytes:
    """The JSON behind a stored blob, for a client that can't take gzip."""
    return gzip.decompress(blob)


async def get_raw(key: str) -> bytes | None:
    """The stored blob, still compressed, ready to hand to a gzip-capable client.

    Deliberately neither parses nor decompresses: the caller returns this as the
    response body. The magic-number check catches a truncated or stale-format
    entry, which should behave exactly like a miss.
    """
    blob = await get_client().get(key)
    if blob is None:
        return None
    if not blob.startswith(_GZIP_MAGIC):
        await get_client().delete(key)
        return None
    return blob


async def set_raw(key: str, blob: bytes, ttl: int | None = None) -> None:
    await get_client().set(key, blob, ex=ttl if ttl is not None else settings.cache_ttl_seconds)


def principal_key(token_hash: str) -> str:
    """Keyed by the token's SHA-256, never by the token itself.

    The plaintext token is a credential and does not belong in a second store.
    The hash is what the database holds, and keying on it here means a Redis
    dump discloses exactly what a database dump already would.
    """
    return f"constella:{CACHE_VERSION}:auth:{token_hash}"


async def get_principal(token_hash: str) -> tuple[str, str] | None:
    """A cached `(student_id, school_id)` for this token, if there is one."""
    blob = await get_client().get(principal_key(token_hash))
    if blob is None:
        return None
    try:
        student_id, _, school_id = blob.decode().partition("\x1f")
    except UnicodeDecodeError:
        return None
    # A tenantless principal must never come back from cache — see
    # `set_principal`, which refuses to store one.
    if not student_id or not school_id:
        return None
    return student_id, school_id


async def set_principal(token_hash: str, student_id: str, school_id: str, ttl: int) -> None:
    """Remember a resolved token for `ttl` seconds.

    Refuses a null tenant. `school_id=None` means *unscoped* everywhere
    downstream, so a cached principal without one would be the fail-open case
    `current_student` exists to prevent — with a TTL attached.
    """
    if not student_id or not school_id or ttl <= 0:
        return
    await get_client().set(
        principal_key(token_hash), f"{student_id}\x1f{school_id}".encode(), ex=ttl
    )


async def forget_principal(token_hash: str) -> None:
    await get_client().delete(principal_key(token_hash))


async def track_student_key(student_id: str, key: str, params: dict | None = None) -> None:
    """Record that `key` belongs to `student_id`, along with the query that
    produced it so the nightly job can rebuild it.

    Pipelined: the write path does this on every miss, and two round trips per
    miss is one more than it needs.
    """
    client = get_client()
    index = student_index_key(student_id)
    entry = json.dumps(
        params or {"kind": KIND_CONSTELLATION}, separators=(",", ":"), ensure_ascii=False
    )
    async with client.pipeline(transaction=False) as pipe:
        pipe.hset(index, key, entry)
        # The index must outlive the entries it points at, or invalidation misses them.
        pipe.expire(index, settings.cache_ttl_seconds * 2)
        await pipe.execute()


async def student_index_entries(student_id: str) -> dict[str, dict]:
    """Every cache key for this student, mapped to the query behind it.

    An entry whose value doesn't parse is returned as an unknown kind rather
    than dropped — invalidation still needs to delete it.
    """
    raw = await get_client().hgetall(student_index_key(student_id))
    entries: dict[str, dict] = {}
    for key, value in (raw or {}).items():
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        # The client hands back bytes; keys travel onward as Redis key strings.
        entries[key.decode()] = parsed if isinstance(parsed, dict) else {}
    return entries


async def invalidate_student(student_id: str, keep: set[str] | None = None) -> int:
    """Drop this student's cached entries, optionally sparing some.

    Called when their profile changes — new courses, a new semester, a new pivot
    query — since all of those change the scores.

    `keep` exists for the nightly job, which overwrites an entry in place and
    then clears the rest. Sparing the freshly written keys means the job never
    opens a window where a student's constellation is missing.
    """
    client = get_client()
    index = student_index_key(student_id)
    keys = {field.decode() for field in await client.hkeys(index) or []}
    stale = keys - (keep or set())
    if not stale:
        return 0
    async with client.pipeline(transaction=False) as pipe:
        pipe.delete(*stale)
        pipe.hdel(index, *stale)
        results = await pipe.execute()
    return int(results[0] or 0)


# Deliberately version-agnostic. Bumping `CACHE_VERSION` makes every older entry
# unreachable but not gone — it still holds memory until its TTL expires, and the
# TTL is now measured in days. A flush that only swept the *current* version
# would leave the entire previous keyspace behind at exactly the moment a deploy
# created it.
FLUSH_PATTERN = "constella:*"


async def invalidate_all_constellations() -> int:
    """Drop every cached entry, of every cache version.

    Called when the alumni corpus changes, which invalidates every student's
    scores at once, and after a `CACHE_VERSION` bump to reclaim what the bump
    stranded. SCAN rather than KEYS so this stays safe on a live server, and
    deletes go out in batches — one round trip per key turns a flush of a large
    keyspace into tens of thousands of sequential awaits.
    """
    client = get_client()
    deleted = 0
    batch: list[bytes] = []
    async for key in client.scan_iter(match=FLUSH_PATTERN, count=500):
        batch.append(key)
        if len(batch) >= 500:
            deleted += await client.delete(*batch)
            batch = []
    if batch:
        deleted += await client.delete(*batch)
    return deleted


# --------------------------------------------------------------------------
# Single-flight
# --------------------------------------------------------------------------


def _lock_key(key: str) -> str:
    return f"{key}:lock"


async def acquire_compute_lock(key: str) -> bool:
    """True if this caller won the right to compute `key`.

    Losers wait on `await_entry` instead of recomputing the same payload. The
    TTL bounds how long a crashed holder can block the others.
    """
    won = await get_client().set(
        _lock_key(key), "1", nx=True, ex=settings.cache_lock_ttl_seconds
    )
    return bool(won)


async def release_compute_lock(key: str) -> None:
    """Release without checking ownership.

    A token compare would be more precise, but the only thing a spurious release
    can cause here is a second caller recomputing a payload that was about to be
    cached anyway. That is not worth a Lua script to prevent.
    """
    await get_client().delete(_lock_key(key))


async def await_entry(key: str, timeout: float | None = None) -> str | None:
    """Poll for the lock holder's result, giving up after `timeout`.

    Returns None on timeout so the caller computes it themselves. Waiting must
    never be able to fail a request — a stalled holder costs latency, not a 500.
    """
    budget = settings.cache_lock_wait_seconds if timeout is None else timeout
    deadline = asyncio.get_running_loop().time() + budget
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        raw = await get_raw(key)
        if raw is not None:
            return raw
    return None


async def ping() -> bool:
    try:
        return bool(await get_client().ping())
    except Exception:
        return False
