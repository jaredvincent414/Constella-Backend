"""Response helpers shared by the cached read paths."""

from __future__ import annotations

from fastapi import Request, Response

from app import cache


def cached_json(blob: bytes, request: Request) -> Response:
    """Serve a cached payload, compressed, without touching it.

    Entries are stored gzipped (`cache.serialize_cached`), which is also the
    encoding a browser wants — so the common path hands Redis's bytes straight
    to the socket and neither parses, revalidates, nor decompresses them.

    A client that doesn't advertise gzip gets it decompressed here rather than a
    body it can't read. `Vary` is set either way, because the same URL now has
    two representations and a shared cache must not serve one for the other.
    """
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        return Response(
            content=blob,
            media_type="application/json",
            headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
        )
    return Response(
        content=cache.decompress(blob),
        media_type="application/json",
        headers={"Vary": "Accept-Encoding"},
    )
