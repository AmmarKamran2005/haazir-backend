"""Sensor fusion. Port of `fuse` and `observe` in `app/assets/js/engine.js`. Plan §6.1, §6.2.

A scalar inverse-variance filter, which is the Kalman update for a one-dimensional static
state. Each source contributes a precision `1 / sigma^2`, decayed exponentially by how stale
that reading is, and the estimate is the precision-weighted mean. The variance of the result
is `1 / total_precision`, so the confidence and the per-source weight bars the interface shows
are the filter's own internals rather than a decoration computed alongside it.

Two things carry over from the prototype unchanged because they were arrived at by fixing
real bugs.

**Sigma is per observation, not per source.** Counting noise is Poisson: the relative error on
N payment ticks is 1/sqrt(N). A 420-cover venue clearing 90 ticks in a window gives a sharp
reading; an 80-cover cash-heavy nihari clearing five gives a nearly useless one. With one
fixed sigma the filter wildly over-trusted small venues and the estimate jumped thirty points
between frames. The `observation.sigma` column exists for this.

**Tau differs by source.** A staff tap stays informative far longer than one payment window.
The prior never goes stale and is never sharp.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# sigma: default measurement sd in occupancy units, used when a row carries none.
# tau:   precision half-life in minutes. None means "never goes stale".
SOURCES: dict[str, dict] = {
    "payment": {"sigma": 0.085, "tau": 24.0, "label": "Payment velocity",
                "sub": "Raast QR merchant ticks"},
    "staff": {"sigma": 0.050, "tau": 42.0, "label": "Staff console",
              "sub": "Venue-side one-tap state"},
    "checkin": {"sigma": 0.140, "tau": 28.0, "label": "Diner check-ins",
                "sub": "GPS + dwell verified"},
    "pos": {"sigma": 0.060, "tau": 30.0, "label": "POS integration",
            "sub": "Point-of-sale covers"},
    "prior": {"sigma": 0.215, "tau": None, "label": "Historical prior",
              "sub": "Same hour-of-week"},
}

# The prior's sd. Confidence is measured against it, so a fused estimate no sharper than the
# baseline scores zero: knowing nothing new must not look like knowing something.
SIGMA_REF = 0.215

# Below this, a decayed source contributes nothing and is dropped rather than carried as a
# vanishing weight that still shows up in the interface's source list. This is the ONLY age
# cutoff in the model, exactly as in the prototype: with tau = 42 a staff tap stays above the
# threshold for about ten hours, by which point its weight is a fraction of a percent anyway.
MIN_PRECISION = 1e-4


# `Estimate.is_live` for the candidate query, which cannot call into Python.
#
# The distinction it encodes: a `live_state` row existing is NOT the same as somebody having
# reported. The refresh job writes a row for every venue, prior-only ones included, so a test
# of "is there a row" labelled almost the whole city live while the source list on the same
# screen said prior. Kept beside the property so a change to one is a visible omission here.
SOURCE_IS_LIVE_SQL = (
    "EXISTS (SELECT 1 FROM jsonb_object_keys(COALESCE(l.source_weights, '{}'::jsonb)) k"
    " WHERE k <> 'prior')"
)


def source_lines(weights: dict | None) -> list[dict]:
    """`{"prior": 1.0}` as the engine panel renders it, heaviest contributor first.

    Lives here because SOURCES does. It is the product's claim that the number can be
    checked rather than merely believed, so both the search rows and the venue card must
    build it the same way — two copies would eventually disagree about what a source is.
    """
    return [
        {
            "source": name,
            "label": SOURCES.get(name, {}).get("label", name),
            "sub": SOURCES.get(name, {}).get("sub", ""),
            "weight": round(float(weight), 4),
        }
        for name, weight in sorted((weights or {}).items(), key=lambda kv: -float(kv[1]))
    ]

# How far back `load_observations` looks. A query window, not a modelling decision: anything
# older is already below MIN_PRECISION against a freshly written prior.
QUERY_WINDOW_MIN = 360.0


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass(frozen=True, slots=True)
class Observation:
    source: str
    value: float
    sigma: float
    observed_at: dt.datetime
    # 1.0 is neutral and is what the prototype implicitly uses: there, a source's
    # reliability lives entirely in its sigma. Production adds this because two check-ins are
    # not equal evidence when one is a week-old account and the other has fifty verified
    # visits, and because a staff report from outside the geofence must be kept for the audit
    # trail while counting for almost nothing. It multiplies precision, which is the same as
    # inflating sigma by 1/sqrt(trust).
    reporter_trust: float = 1.0
    geo_ok: bool | None = None


@dataclass(frozen=True, slots=True)
class SourcePart:
    """One source's contribution, exactly as the engine panel renders it."""

    source: str
    value: float
    sigma: float
    age_min: float
    precision: float
    weight: float


@dataclass(slots=True)
class Fused:
    occupancy: float
    sd: float
    confidence: float
    trend: float = 0.0
    parts: list[SourcePart] = field(default_factory=list)
    stale_min: float | None = None

    @property
    def source_weights(self) -> dict[str, float]:
        return {p.source: round(p.weight, 4) for p in self.parts}

    @property
    def is_live(self) -> bool:
        """True when something other than the prior is contributing."""
        return any(p.source != "prior" for p in self.parts)


def fuse(
    observations: list[Observation],
    now: dt.datetime,
    *,
    calibration: dict[str, dict] | None = None,
) -> Fused | None:
    """One observation per source in, one estimate out.

    `observations` should already be reduced to the most recent reading per source, which is
    what `latest_per_source` does. Passing several of one source would double-count it: the
    filter treats every input as independent evidence, and two readings from the same tablet
    ninety seconds apart are not.
    """
    num = 0.0
    den = 0.0
    parts: list[SourcePart] = []

    for obs in observations:
        spec = SOURCES.get(obs.source)
        if spec is None:
            continue

        age_min = max(0.0, (now - obs.observed_at).total_seconds() / 60.0)
        tau = spec["tau"]
        decay = 1.0 if tau is None else math.exp(-age_min / tau)

        sigma = obs.sigma or spec["sigma"]
        value = obs.value
        if calibration and obs.source in calibration:
            cal = calibration[obs.source]
            # A venue whose staff always tap "busy" gets corrected rather than distrusted
            # wholesale. Learned per venue and per source by the nightly job.
            value = clamp(value - float(cal.get("bias", 0.0)), 0.0, 1.0)
            sigma = math.sqrt(sigma * sigma + float(cal.get("variance", 0.0)))

        # A report from outside the geofence is kept for the audit trail and weighted to
        # almost nothing, which is what `reporter_trust` already encodes.
        trust = clamp(obs.reporter_trust, 0.01, 1.0)
        precision = (decay * trust) / (sigma * sigma)
        if precision < MIN_PRECISION:
            continue

        num += precision * value
        den += precision
        parts.append(
            SourcePart(
                source=obs.source, value=value, sigma=sigma,
                age_min=age_min, precision=precision, weight=0.0,
            )
        )

    if den == 0.0:
        return None

    x = num / den
    sd = math.sqrt(1.0 / den)

    weighted = [
        SourcePart(
            source=p.source, value=p.value, sigma=p.sigma, age_min=p.age_min,
            precision=p.precision, weight=p.precision / den,
        )
        for p in parts
    ]
    weighted.sort(key=lambda p: -p.weight)

    live_ages = [p.age_min for p in weighted if p.source != "prior"]
    return Fused(
        occupancy=clamp(x, 0.0, 1.0),
        sd=sd,
        confidence=clamp(1.0 - sd / SIGMA_REF, 0.0, 0.985),
        parts=weighted,
        stale_min=min(live_ages) if live_ages else None,
    )


def latest_per_source(observations: list[Observation]) -> list[Observation]:
    """Newest reading of each source. See the note in `fuse` on why this matters."""
    newest: dict[str, Observation] = {}
    for obs in observations:
        current = newest.get(obs.source)
        if current is None or obs.observed_at > current.observed_at:
            newest[obs.source] = obs
    return list(newest.values())


def trend_per_hour(history: list[tuple[dt.datetime, float]], now: dt.datetime,
                   window_min: float = 25.0) -> float:
    """Least-squares slope of recent fused occupancy, in occupancy units per hour.

    The sign is what the interface reads as "filling up" or "emptying out", so a flat or
    two-point history returns zero rather than a slope fitted to noise.
    """
    recent = [(t, x) for t, x in history if (now - t).total_seconds() / 60.0 <= window_min]
    if len(recent) < 3:
        return 0.0

    times = [(t - now).total_seconds() / 60.0 for t, _ in recent]
    values = [x for _, x in recent]
    mt = sum(times) / len(times)
    mx = sum(values) / len(values)

    sxy = sum((t - mt) * (x - mx) for t, x in zip(times, values, strict=True))
    sxx = sum((t - mt) ** 2 for t in times)
    return (sxy / sxx) * 60.0 if sxx > 0 else 0.0


# --- database access ---------------------------------------------------------


async def load_observations(
    session: AsyncSession, venue_id: uuid.UUID, now: dt.datetime | None = None
) -> list[Observation]:
    """The newest observation of each source for one venue.

    `DISTINCT ON` rather than a window function because it is the cheaper plan on
    `observation_venue_time`, and this runs once per venue per refresh cycle.
    """
    now = now or dt.datetime.now(dt.UTC)
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT ON (source)
                       source, value, sigma, observed_at, reporter_trust, geo_ok
                  FROM observation
                 WHERE venue_id = :v
                   AND observed_at > :since
                 ORDER BY source, observed_at DESC
                """
            ),
            {"v": venue_id, "since": now - dt.timedelta(minutes=QUERY_WINDOW_MIN)},
        )
    ).mappings().all()

    return [
        Observation(
            source=r["source"], value=float(r["value"]), sigma=float(r["sigma"]),
            observed_at=r["observed_at"], reporter_trust=float(r["reporter_trust"]),
            geo_ok=r["geo_ok"],
        )
        for r in rows
    ]


async def load_calibration(session: AsyncSession, venue_id: uuid.UUID) -> dict[str, dict]:
    rows = (
        await session.execute(
            text(
                "SELECT source, bias, variance, trust, n FROM source_calibration "
                "WHERE venue_id = :v"
            ),
            {"v": venue_id},
        )
    ).mappings().all()
    return {r["source"]: dict(r) for r in rows}
