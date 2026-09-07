"""Wait time from occupancy. Port of `waitAt`, `waitFrom`, `stateBand` and
`waitDropsBelow` in `app/assets/js/engine.js`. Plan §6.3.

Below about 72% utilisation you are seated on arrival and the small residual is the walk to
the table. Past that, wait grows like `rho^4 / (1 - rho)`, which is the shape an M/M/c queue
takes as it saturates: the last few percent of a dining room cost far more than the first
eighty.

The band is the fused variance pushed through the same curve rather than a margin added to
the answer. `p90` is the wait at `x + 1.281 sd`, because 1.281 is the 90th percentile of the
standard normal and the fused posterior is Gaussian by construction. Widening the interval by
a flat number of minutes instead would break the one property that makes it worth showing:
that it narrows when the estimate is well sourced.
"""

from __future__ import annotations

import math

# Small rooms queue harder: the same 90% full is a longer wait at a 60-cover nihari counter
# than at a 420-cover hall, because there are fewer tables turning over.
TURN_LARGE = 1.15   # capacity > 300
TURN_MEDIUM = 1.55
TURN_SMALL = 2.1    # capacity < 90
DEFAULT_CAPACITY = 120

FREE_FLOW_CEILING = 0.72
MAX_WAIT_MIN = 95.0

# 90th percentile of the standard normal.
Z90 = 1.281


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def turn_factor(capacity: int | None) -> float:
    c = capacity or DEFAULT_CAPACITY
    if c > 300:
        return TURN_LARGE
    if c < 90:
        return TURN_SMALL
    return TURN_MEDIUM


def wait_at(rho: float, capacity: int | None = None) -> float:
    """Minutes of wait at occupancy `rho`."""
    r = clamp(rho, 0.0, 0.995)
    if r < FREE_FLOW_CEILING:
        # Not zero: at 60% there is still a walk to a table and a menu to be brought.
        return max(0.0, (r - 0.5) * 6.0)
    turn = turn_factor(capacity)
    return clamp(turn * (r**4) / max(1.0 - r, 0.035), 0.0, MAX_WAIT_MIN)


def wait_band(x: float, sd: float, capacity: int | None = None) -> dict[str, float]:
    """p50, the one-sigma interval, and p90, all through the same curve."""
    return {
        "p50": wait_at(x, capacity),
        "lo": wait_at(max(0.0, x - sd), capacity),
        "hi": wait_at(min(0.995, x + sd), capacity),
        "p90": wait_at(min(0.995, x + Z90 * sd), capacity),
    }


def state_band(x: float) -> str:
    """The four words the interface is allowed to say.

    These thresholds are higher than the ones the city map uses for an area average, and
    deliberately: 60% of a single restaurant is a comfortable room, while 60% across a whole
    neighbourhood means most of it is busy.
    """
    if x < 0.55:
        return "free"
    if x < 0.80:
        return "moderate"
    if x < 0.92:
        return "busy"
    return "full"


def wait_drops_below(
    current_occupancy: float,
    prior_curve: list[float],
    hour_of_week_now: int,
    target_min: float,
    capacity: int | None = None,
    *,
    horizon_min: int = 300,
    step_min: int = 5,
) -> dict | None:
    """When the wait next falls under `target_min`, or None inside the horizon.

    The projection is the venue's own prior curve carried forward, anchored on how far
    today is currently running from it. That offset decays with a 90-minute time constant:
    a room that is unusually full right now is probably still unusual in twenty minutes and
    probably not in three hours. Projecting the raw prior instead would promise a quiet
    restaurant to anyone standing in a queue that the prior does not know about.
    """
    if wait_at(current_occupancy, capacity) <= target_min:
        return None

    anchor_offset = current_occupancy - prior_curve[hour_of_week_now % 168]

    for dt_min in range(step_min * 2, horizon_min + 1, step_min):
        future_how = (hour_of_week_now + dt_min // 60) % 168
        projected = clamp(
            prior_curve[future_how] + anchor_offset * math.exp(-dt_min / 90.0), 0.0, 0.995
        )
        if wait_at(projected, capacity) < target_min:
            return {"in_min": dt_min, "hour_of_week": future_how, "occupancy": projected}
    return None


def occupancy_for_wait(target_min: float, capacity: int | None = None) -> float:
    """Invert the curve: the occupancy that produces this wait.

    Used when staff report a state *and* a wait. The band alone is four buckets; a typed
    "25 minutes" is a much sharper statement, and this is what turns it back into the
    occupancy units the filter works in.
    """
    best, best_err = 0.72, float("inf")
    r = 0.40
    while r < 0.995:
        err = abs(wait_at(r, capacity) - target_min)
        if err < best_err:
            best, best_err = r, err
        r += 0.005
    return best
