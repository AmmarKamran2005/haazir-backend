"""Background recomputation. Plan §11.

`refresh_live_state` is the loop that makes the product live. Every cycle it writes a fresh
`prior` observation for each venue, fuses whatever else has arrived, and stores the result in
`live_state` for the read endpoints to serve.

Writing the prior as an observation rather than special-casing it in the fuser is the point.
It means the baseline competes with the sensors on identical terms, its weight appears in the
same bar chart, and a venue nobody has reported on gets an estimate whose provenance is
visible instead of an empty panel. It is also what makes confidence honest: a venue running
only on its prior scores zero, because `SIGMA_REF` is the prior's own spread.

The job only touches venues that could have changed. Re-fusing seventeen hundred venues a
minute to rewrite identical numbers would spend the whole Neon compute allowance on arithmetic
nobody asked for.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..estimator import fusion, queueing, trust
from ..estimator.fusion import Observation

log = logging.getLogger("haazir.recompute")

# A venue is refreshed if an observation landed since the last refresh, or if its stored state
# is older than this. The floor exists because the prior itself moves with the clock: a venue
# nobody reports on still fills up at eight in the evening.
STALE_STATE_MIN = 15

# The prior's sd, from the estimator's source table.
PRIOR_SIGMA = fusion.SOURCES["prior"]["sigma"]


async def write_prior_observations(
    session: AsyncSession, venue_ids: list[uuid.UUID] | None = None
) -> int:
    """Insert this hour's prior as a first-class observation for each venue.

    Idempotent within the hour: a venue that already has a prior observation from the current
    hour is skipped, so running the job every minute does not stack sixty identical readings
    and manufacture certainty out of repetition.
    """
    where = "AND v.id = ANY(CAST(:ids AS uuid[]))" if venue_ids else ""
    result = await session.execute(
        text(
            f"""
            INSERT INTO observation (venue_id, source, observed_at, value, sigma,
                                     reporter_trust, payload)
            SELECT v.id, 'prior', now(), p.mean_ratio, GREATEST(p.sigma, 0.01), 1.0,
                   jsonb_build_object('hour_of_week', p.hour_of_week, 'from', p.source)
              FROM venue v
              JOIN city c ON c.id = v.city_id
              JOIN occupancy_prior p
                ON p.venue_id = v.id
               AND p.hour_of_week = (EXTRACT(DOW  FROM now() AT TIME ZONE c.timezone)::int * 24
                                   + EXTRACT(HOUR FROM now() AT TIME ZONE c.timezone)::int)
             WHERE v.status = 'active' {where}
               AND NOT EXISTS (
                   SELECT 1 FROM observation o
                    WHERE o.venue_id = v.id
                      AND o.source = 'prior'
                      AND o.observed_at > date_trunc('hour', now())
               )
            """
        ),
        {"ids": [str(v) for v in venue_ids]} if venue_ids else {},
    )
    return result.rowcount or 0


async def venues_needing_refresh(session: AsyncSession, limit: int = 500) -> list[uuid.UUID]:
    rows = await session.execute(
        text(
            """
            SELECT v.id
              FROM venue v
              LEFT JOIN live_state l ON l.venue_id = v.id
             WHERE v.status = 'active'
               AND (
                   l.venue_id IS NULL
                   OR l.updated_at < now() - make_interval(mins => :stale)
                   OR EXISTS (
                       SELECT 1 FROM observation o
                        WHERE o.venue_id = v.id
                          AND o.observed_at > l.updated_at
                   )
               )
             ORDER BY l.updated_at NULLS FIRST
             LIMIT :limit
            """
        ),
        {"stale": STALE_STATE_MIN, "limit": limit},
    )
    return [r[0] for r in rows]


async def refresh_live_state(
    session: AsyncSession, venue_ids: list[uuid.UUID] | None = None,
    now: dt.datetime | None = None,
) -> int:
    """Fuse and store. Returns how many venues were written."""
    now = now or dt.datetime.now(dt.UTC)
    if venue_ids is None:
        venue_ids = await venues_needing_refresh(session)
    if not venue_ids:
        return 0

    await write_prior_observations(session, venue_ids)

    # All observations for the batch in one query rather than one per venue.
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT ON (venue_id, source)
                       venue_id, source, value, sigma, observed_at, reporter_trust
                  FROM observation
                 WHERE venue_id = ANY(CAST(:ids AS uuid[]))
                   AND observed_at > :since
                 ORDER BY venue_id, source, observed_at DESC
                """
            ),
            {
                "ids": [str(v) for v in venue_ids],
                "since": now - dt.timedelta(minutes=fusion.QUERY_WINDOW_MIN),
            },
        )
    ).mappings().all()

    by_venue: dict[uuid.UUID, list[Observation]] = {}
    for r in rows:
        by_venue.setdefault(r["venue_id"], []).append(
            Observation(
                source=r["source"], value=float(r["value"]), sigma=float(r["sigma"]),
                observed_at=r["observed_at"], reporter_trust=float(r["reporter_trust"]),
            )
        )

    capacities = dict(
        (
            await session.execute(
                text(
                    "SELECT id, capacity_covers FROM venue "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [str(v) for v in venue_ids]},
            )
        ).all()
    )

    writes = []
    for venue_id, observations in by_venue.items():
        fused = fusion.fuse(fusion.latest_per_source(observations), now)
        if fused is None:
            continue
        capacity = capacities.get(venue_id)
        band = queueing.wait_band(fused.occupancy, fused.sd, capacity)
        writes.append(
            {
                "v": venue_id,
                "occ": fused.occupancy,
                "sd": fused.sd,
                "p50": band["p50"],
                "p90": band["p90"],
                "trend": fused.trend,
                "conf": fused.confidence,
                "band": queueing.state_band(fused.occupancy),
                "weights": json.dumps(fused.source_weights),
            }
        )

    if not writes:
        return 0

    await session.execute(
        text(
            """
            INSERT INTO live_state (venue_id, occupancy, sd, wait_p50_min, wait_p90_min,
                                    trend, confidence, band, source_weights, updated_at)
            VALUES (:v, :occ, :sd, :p50, :p90, :trend, :conf, :band,
                    CAST(:weights AS jsonb), now())
            ON CONFLICT (venue_id) DO UPDATE SET
                occupancy = EXCLUDED.occupancy,
                sd = EXCLUDED.sd,
                wait_p50_min = EXCLUDED.wait_p50_min,
                wait_p90_min = EXCLUDED.wait_p90_min,
                trend = EXCLUDED.trend,
                confidence = EXCLUDED.confidence,
                band = EXCLUDED.band,
                source_weights = EXCLUDED.source_weights,
                updated_at = now()
            """
        ),
        writes,
    )
    return len(writes)


async def decay_facts(session: AsyncSession) -> int:
    """Nightly. A fact nobody re-confirms loses weight. §3.4.

    `c := c * exp(-days_since_verified / 180)`, floored at 0.25. The floor matters: a fact
    should fade toward "we are no longer sure" and never to zero, because a decayed
    observation is still evidence and dropping it entirely would throw away the only thing
    anyone ever told us about that venue.
    """
    result = await session.execute(
        text(
            """
            UPDATE venue SET attributes = (
                SELECT jsonb_object_agg(
                    key,
                    CASE WHEN value ? 'at' AND value ? 'c'
                         THEN value || jsonb_build_object('c', GREATEST(0.25,
                              (value->>'c')::float *
                              exp(-GREATEST(0, (CURRENT_DATE - (value->>'at')::date)) / 180.0)))
                         ELSE value END)
                  FROM jsonb_each(attributes)
            )
            WHERE attributes <> '{}'::jsonb
            """
        )
    )
    return result.rowcount or 0


async def recompute_trust(session: AsyncSession, limit: int = 2000) -> int:
    """Nightly. Scores every venue and stores the breakdown."""
    venue_ids = [
        r[0]
        for r in await session.execute(
            text("SELECT id FROM venue WHERE status = 'active' LIMIT :limit"),
            {"limit": limit},
        )
    ]
    for venue_id in venue_ids:
        score = await trust.compute_for_venue(session, venue_id)
        await trust.store(session, venue_id, score)
    return len(venue_ids)


async def create_next_partition(session: AsyncSession) -> str:
    """Monthly. An insert with no matching partition raises, and the insert path is the staff
    console during a live demo."""
    return await session.scalar(
        text("SELECT ensure_observation_partition((now() + INTERVAL '2 months')::date)")
    )
