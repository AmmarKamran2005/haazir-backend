"""A rate limit on every write, applied by middleware. Plan §12 Phase 10.

The plan asks for "rate limits everywhere". The obvious way to get there is a
`ratelimit.check` call at the top of each handler, and the problem with it is that the
twenty-second endpoint somebody adds next month will not have one, and nothing will say so.
A middleware cannot be forgotten.

Per-endpoint limits still exist where the rule is about meaning rather than volume: one
check-in per person per venue per 45 minutes is a statement about what a check-in *is*, not a
defence against a flood, and it belongs next to the code that knows that. This layer is the
floor underneath those.

**Reads are not limited here.** Browsing is never gated (§5), and a cap on GETs would hit the
city map polling for the live feed long before it hit anybody abusing the API. Search and ask
are POSTs but are reads, so they get their own generous ceiling rather than the write one.
"""

from __future__ import annotations

import logging
import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..config import settings
from . import ratelimit

log = logging.getLogger("haazir.throttle")

WINDOW_SECONDS = 60

# Default ceiling for any write, per IP per minute. Generous: this is a floor against abuse,
# not a quota. A real person filling in a group constraint form does not come close.
DEFAULT_WRITE_LIMIT = 40

# Tighter or looser where the cost or the risk differs. First match wins.
RULES: list[tuple[re.Pattern, int]] = [
    # Reads that happen to be POSTs. Each runs a real query, so not unlimited, but a diner
    # refining a search several times a minute is normal behaviour.
    (re.compile(r"^/v1/(search|ask)$"), 60),
    # Auth. The magic-link endpoint also has a database-backed per-email and per-IP limit
    # (§5 rule 4); this is the cheap outer guard that stops a flood reaching it at all.
    (re.compile(r"^/v1/auth/request-link$"), 12),
    (re.compile(r"^/v1/auth/(verify|refresh)$"), 30),
    # Writes that create rows somebody else has to look at.
    (re.compile(r"^/v1/groups$"), 10),
    (re.compile(r"^/v1/venues/[^/]+/facts$"), 20),
    # Bulk ingestion is admin-only and legitimately large; a low cap here would break a load.
    (re.compile(r"^/v1/admin/ingest/"), 120),
]


def limit_for(path: str) -> int:
    for pattern, limit in RULES:
        if pattern.match(path):
            return limit
    return DEFAULT_WRITE_LIMIT


def client_ip(request: Request) -> str:
    """Fly.io terminates TLS and forwards the real address. Without reading its header every
    request looks like it came from the proxy and the limit protects nothing."""
    for header in ("fly-client-ip", "cf-connecting-ip", "x-real-ip"):
        value = request.headers.get(header)
        if value:
            return value.split(",")[0].strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class ThrottleMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.rate_limit_enabled:
            return await call_next(request)
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)

        path = request.url.path
        limit = limit_for(path)
        # Keyed on the path itself rather than the route template, which the middleware
        # cannot see. Slightly coarser for parameterised routes and harmless: the caller is
        # already scoped by IP.
        key = f"throttle:{client_ip(request)}:{path}"

        try:
            ratelimit.check(key, limit=limit, window_s=WINDOW_SECONDS)
        except ratelimit.RateLimited as exc:
            log.info("throttled %s on %s", key, path)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Too many requests. Slow down and try again shortly.",
                    "limit_per_minute": limit,
                },
                headers={"Retry-After": str(exc.retry_after)},
            )

        return await call_next(request)
