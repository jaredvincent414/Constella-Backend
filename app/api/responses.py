"""Response helpers shared by the cached read paths."""

from __future__ import annotations

import hashlib

from fastapi import Request, Response

from app import cache


def _etag(blob: bytes, gzipped: bool) -> str:
    """A tag for this exact representation.

    Derived from the stored bytes, which are byte-stable for a given payload
    (`cache.serialize_cached` fixes gzip's mtime), so the tag only changes when
    the content does — which for a constellation means when the nightly job
    reruns.

    The encoding is part of the tag because it is part of the representation: a
    gzip body and an identity body of the same payload are different entities,
    and an intermediary must not answer one with the other's tag.
    """
    digest = hashlib.sha256(blob).hexdigest()[:16]
    return f'"{digest}{"-gz" if gzipped else ""}"'


def _matches(header: str | None, etag: str) -> bool:
    """RFC 9110 If-None-Match: `*`, or a comma-separated list of tags.

    Weak comparison, so a `W/`-prefixed tag still matches — we never emit one,
    but a proxy may rewrite ours.
    """
    if not header:
        return False
    candidates = [tag.strip() for tag in header.split(",")]
    if "*" in candidates:
        return True
    return any(tag.removeprefix("W/") == etag for tag in candidates)


def cached_json(blob: bytes, request: Request) -> Response:
    """Serve a cached payload, compressed, without touching it.

    Entries are stored gzipped (`cache.serialize_cached`), which is also the
    encoding a browser wants — so the common path hands Redis's bytes straight
    to the socket and neither parses, revalidates, nor decompresses them.

    A client that doesn't advertise gzip gets it decompressed here rather than a
    body it can't read. `Vary` is set either way, because the same URL now has
    two representations and a shared cache must not serve one for the other.

    A returning client that already has the payload gets a 304 with no body at
    all. The constellation only changes when the job reruns, so revalidation is
    almost always a 304 — which is the difference between 4 KB and ~0 for the
    common case of a student navigating back to a view they already loaded.
    `no-cache` means "cache this, but always revalidate": never stale, and
    `private` because every payload is scored for one student.
    """
    gzipped = "gzip" in request.headers.get("accept-encoding", "").lower()
    etag = _etag(blob, gzipped)
    headers = {
        "ETag": etag,
        "Cache-Control": "private, no-cache",
        "Vary": "Accept-Encoding",
    }

    if _matches(request.headers.get("if-none-match"), etag):
        # 304 carries no body, and must not carry Content-Encoding either —
        # there is no content to have been encoded.
        return Response(status_code=304, headers=headers)

    if gzipped:
        return Response(
            content=blob,
            media_type="application/json",
            headers={**headers, "Content-Encoding": "gzip"},
        )
    return Response(content=cache.decompress(blob), media_type="application/json", headers=headers)
