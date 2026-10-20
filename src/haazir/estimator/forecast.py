"""The next few hours. Plan §4 B5.

`prior[h + k] + (live_now - prior[h]) * exp(-k / tau)`, with tau about 45 minutes. Whatever
the room is doing differently from its usual week right now fades back into the usual week:
a Tuesday that is unexpectedly full at eight is probably still busier than usual at half past,
and by eleven it is just Tuesday again. The wait comes from the same queueing curve the live
estimate uses, so the forecast and the live card cannot disagree about what 80% means.
"""

from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

from . import queueing

TAU_MIN = 45.0
STEP_MIN = 30
# A venue with no prior row for an hour (a gap in the archetype) reads as a quiet room.
DEFAULT_PRIOR = 0.3


def _how(local: dt.datetime) -> int:
    """Hour of week, 0 = Sunday 00:00, the convention `occupancy_prior` is stored in."""
    return ((local.weekday() + 1) % 7) * 24 + local.hour


def prior_at(prior: dict[int, float], local: dt.datetime) -> float:
    """Linear between hour-of-week rows, so a 30-minute step is not a staircase."""
    h = _how(local)
    here = prior.get(h, DEFAULT_PRIOR)
    nxt = prior.get((h + 1) % 168, here)
    frac = local.minute / 60.0
    return here + (nxt - here) * frac


def project(
    prior: dict[int, float],
    live_now: float | None,
    hours: int,
    capacity: int | None,
    now: dt.datetime | None = None,
    tz: str = "Asia/Karachi",
) -> dict:
    """`{now, points}` in the endpoint's shape. Pure: the router supplies the numbers."""
    local = (now or dt.datetime.now(dt.UTC)).astimezone(ZoneInfo(tz))
    base_now = prior_at(prior, local)
    current = base_now if live_now is None else live_now
    deviation = current - base_now

    # Steps land on the half hour, which is how people say times.
    start = local.replace(second=0, microsecond=0)
    start += dt.timedelta(minutes=(STEP_MIN - start.minute % STEP_MIN) % STEP_MIN or STEP_MIN)

    points = []
    at = start
    while at <= local + dt.timedelta(hours=hours):
        k = (at - local).total_seconds() / 60.0
        occ = queueing.clamp(prior_at(prior, at) + deviation * math.exp(-k / TAU_MIN), 0.0, 1.0)
        points.append(
            {
                "at": at.isoformat(),
                "occupancy": round(occ, 3),
                "band": queueing.state_band(occ),
                "wait_min": round(queueing.wait_at(occ, capacity)),
            }
        )
        at += dt.timedelta(minutes=STEP_MIN)

    return {
        "now": {"occupancy": round(current, 3), "band": queueing.state_band(current)},
        "points": points,
    }


def at_time(forecast: dict, when: dt.datetime) -> dict | None:
    """The point nearest a requested time, for the chat's "will it be busy at 9?"."""
    if not forecast["points"]:
        return None
    return min(
        forecast["points"],
        key=lambda p: abs((dt.datetime.fromisoformat(p["at"]) - when).total_seconds()),
    )
