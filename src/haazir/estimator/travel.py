"""Travel time. Port of `travelMin` in `app/assets/js/engine.js`, plus the real router.
Plan §6, `travelMin -> estimator/travel.py`: *replace the straight-line approximation with
OSRM, cached*.

Two implementations behind one function.

`osrm_minutes` asks a real routing engine and is what production uses. `estimate_minutes` is
the straight-line fallback the prototype used, and it is not a placeholder: OSRM is a
self-hosted service that can be down, slow, or not yet deployed, and a search that returns
nothing because the router timed out is worse than one whose travel figures are approximate.
The fallback is always available and the response says which was used.

The congestion curve is the part worth keeping either way. Karachi's evening peak runs
18:00-21:00 with a second bump around 13:30, and a straight-line estimate that ignores it is
wrong by 60% at exactly the hour most people are deciding where to eat.
"""

from __future__ import annotations

import datetime as dt
import logging
import math

import httpx

from ..config import settings

log = logging.getLogger("haazir.travel")

FREE_FLOW_KMH = 28.0
# Fixed overhead: parking, walking in, the last hundred metres a router never models.
OVERHEAD_MIN = 4.0

_TIMEOUT = httpx.Timeout(2.5, connect=1.0)
# A router that has not answered in two and a half seconds has already cost more than the
# accuracy it was going to buy. Fall back rather than hold up the whole search.


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def congestion_factor(hour: float) -> float:
    """Multiplier on free-flow time. Two Gaussian bumps, evening and lunch."""
    evening = 0.62 * math.exp(-((hour - 19.5) ** 2) / 5.5)
    lunch = 0.22 * math.exp(-((hour - 13.5) ** 2) / 3.0)
    return 1.0 + evening + lunch


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def estimate_minutes(
    lat1: float, lng1: float, lat2: float, lng2: float, at: dt.datetime | None = None
) -> float:
    """Straight-line distance, road-network detour, congestion, overhead."""
    km = haversine_km(lat1, lng1, lat2, lng2)
    # Karachi's grid is not a grid. A straight line understates driving distance by roughly a
    # third, which is the standard circuity factor for a dense unplanned city.
    km *= 1.35
    hour = (at or dt.datetime.now(dt.UTC)).hour + (at or dt.datetime.now(dt.UTC)).minute / 60
    base = km / FREE_FLOW_KMH * 60.0
    return max(4.0, round(base * congestion_factor(hour) + OVERHEAD_MIN))


async def osrm_minutes(
    lat1: float, lng1: float, lat2: float, lng2: float, at: dt.datetime | None = None
) -> float | None:
    """Real driving time from OSRM, or None if it is unavailable.

    OSRM returns free-flow duration; it has no idea it is half past seven on a Friday in
    Karachi. The congestion factor is applied on top, which is why it lives outside both
    implementations.
    """
    if not settings.osrm_url:
        return None

    url = (
        f"{settings.osrm_url.rstrip('/')}/route/v1/driving/"
        f"{lng1},{lat1};{lng2},{lat2}?overview=false"
    )
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return None
        payload = response.json()
        if payload.get("code") != "Ok" or not payload.get("routes"):
            return None
        seconds = float(payload["routes"][0]["duration"])
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.warning("OSRM unavailable, falling back to the estimate: %s", exc)
        return None

    now = at or dt.datetime.now(dt.UTC)
    hour = now.hour + now.minute / 60
    return max(4.0, round(seconds / 60.0 * congestion_factor(hour) + OVERHEAD_MIN))


async def travel_minutes(
    lat1: float, lng1: float, lat2: float, lng2: float, at: dt.datetime | None = None
) -> tuple[float, str]:
    """`(minutes, source)`. Source is `osrm` or `estimate`, and the API passes it through."""
    routed = await osrm_minutes(lat1, lng1, lat2, lng2, at)
    if routed is not None:
        return routed, "osrm"
    return estimate_minutes(lat1, lng1, lat2, lng2, at), "estimate"


# SQL for the straight-line estimate, so a search over 1,700 venues does not become 1,700
# round trips. PostGIS computes the distance; the congestion factor and overhead are applied
# to the result. OSRM is used to refine only the handful of venues that survive ranking.
TRAVEL_MINUTES_SQL = f"""
    GREATEST(4.0,
        ROUND(
            (ST_Distance(v.geom, ST_SetSRID(ST_MakePoint(:from_lng, :from_lat), 4326)::geography)
             / 1000.0 * 1.35 / {FREE_FLOW_KMH} * 60.0) * :congestion + {OVERHEAD_MIN}
        )
    )
"""
