"""A short in-process cache for public reads, in front of the database entirely.

It sits in middleware rather than in the handlers on purpose: every handler that takes `Ctx`
opens a session and writes its RLS claims before any code runs, so a cache inside a handler
would still cost a round trip, and still wake a suspended database. Here a hit never reaches
the session at all.

**Only what everybody would see.** Admin requests (which RLS shows more to, and which see
`is_demo` on deals) and device or camera credentials bypass it. Every other caller sees the
same public rows for these paths, signed in or not.

**Live data is not allowed to go stale under a demo.** Anything a venue publishes on its live
feed (a staff tap, a camera reading) clears the cached per-venue reads, so the card a phone
refetches after the room changes is the new one.
"""

from __future__ import annotations

import re
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Path pattern, seconds. First match wins.
RULES: list[tuple[re.Pattern, int]] = [
    (re.compile(r"^/v1/venues/[^/]+/live/stream$"), 0),
    (re.compile(r"^/v1/venues/[^/]+/tables$"), 0),
    (re.compile(r"^/v1/venues$"), 60),
    (re.compile(r"^/v1/venues/[^/]+$"), 30),
    (re.compile(r"^/v1/venues/[^/]+/(dishes|forecast|trust)$"), 60),
    (re.compile(r"^/v1/dishes/"), 60),
    (re.compile(r"^/v1/deals$"), 60),
]
MAX_ENTRIES = 2000
MAX_BODY = 512 * 1024

_store: dict[str, tuple[float, int, bytes, str]] = {}


def ttl_for(path: str) -> int:
    for pattern, ttl in RULES:
        if pattern.match(path):
            return ttl
    return 0


def _cacheable_caller(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth:
        return True
    token = auth.partition(" ")[2].strip()
    if token.startswith(("hzd_", "hzc_")):
        return False
    try:
        from ..auth.jwt import decode

        return decode(token).role != "admin"
    except Exception:  # noqa: BLE001 — an invalid token gets its 401 from the handler
        return False


def invalidate_venues() -> None:
    """Drop every cached per-venue read. Called when any venue publishes a live change."""
    # Dish prices too: an owner's menu edit is a write like any other.
    for key in [k for k in _store if k.startswith(("/v1/venues/", "/v1/dishes/"))]:
        _store.pop(key, None)


def clear() -> None:
    _store.clear()


# POSTs that are reads: they change nothing a cached venue read shows.
READ_POSTS = re.compile(r"^/v1/(search|search/answer|ask|chat|ghar/.*)$")


class CacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        from ..config import settings

        if not settings.response_cache_enabled:
            return await call_next(request)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            response = await call_next(request)
            # A write may change what a venue's card, tables or forecast says: a staff tap,
            # a camera reading, an owner's edit. Simplest correct answer: forget them.
            if response.status_code < 400 and not READ_POSTS.match(request.url.path):
                invalidate_venues()
            return response
        if request.method != "GET":
            return await call_next(request)
        path = request.url.path
        ttl = ttl_for(path)
        if not ttl or not _cacheable_caller(request):
            return await call_next(request)

        key = f"{path}?{request.url.query}"
        now = time.monotonic()
        hit = _store.get(key)
        if hit and hit[0] > now:
            _, code, body, media = hit
            return Response(content=body, status_code=code, media_type=media,
                            headers={"X-Cache": "hit"})

        response = await call_next(request)
        if response.status_code != 200:
            return response
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        if len(body) <= MAX_BODY:
            if len(_store) >= MAX_ENTRIES:
                _store.clear()
            _store[key] = (now + ttl, response.status_code, body,
                           response.media_type or headers.get("content-type", "application/json"))
        return Response(content=body, status_code=response.status_code, headers=headers)
