"""Whether anybody is using the API right now, so the database may sleep when nobody is.

Neon suspends a compute after five idle minutes, and bills for every minute it is awake. A
timer that queries every sixty seconds keeps it awake forever: that, with one Fly machine
always on, was what spent October's whole compute allowance by the 21st. So the background
refresh asks this module first and does no database work at all while the API is idle.

"Activity" is a real request or an ingest. The platform's health probe is not activity: it
calls every thirty seconds and would keep the database awake by itself.
"""

from __future__ import annotations

import time

# Past this with no request, the refresh timer stands down and the database can suspend.
IDLE_AFTER_S = 600

_last: float = 0.0


def touch() -> bool:
    """Record activity. Returns True when this ends an idle period (the API just woke)."""
    global _last
    now = time.monotonic()
    woke = now - _last > IDLE_AFTER_S
    _last = now
    return woke


def idle() -> bool:
    return time.monotonic() - _last > IDLE_AFTER_S


def seconds_since() -> float:
    return time.monotonic() - _last


def ignored(path: str) -> bool:
    return path.startswith("/health") or path in ("/docs", "/openapi.json")
