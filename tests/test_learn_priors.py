"""The learning job moves a prior toward what was observed, and is idempotent."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from haazir.db import service_session
from haazir.services import recompute

from .conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


async def test_three_evenings_of_camera_readings_move_that_hour(clean_db, venue_factory):
    venue_id = await venue_factory()
    now = dt.datetime.now(dt.UTC).replace(minute=10, second=0, microsecond=0)
    async with service_session() as s:
        await s.execute(
            text("INSERT INTO occupancy_prior (venue_id, hour_of_week, mean_ratio, sigma, source) "
                 "SELECT :v, h, 0.3, 0.2, 'archetype' FROM generate_series(0, 167) h"),
            {"v": venue_id},
        )
        for weeks in (1, 2, 3):
            await s.execute(
                text("INSERT INTO observation (venue_id, source, observed_at, value, sigma, "
                     "reporter_trust, geo_ok) VALUES (:v, 'camera', :at, 0.9, 0.05, 0.95, TRUE)"),
                {"v": venue_id, "at": now - dt.timedelta(weeks=weeks)},
            )
    for _ in range(2):  # twice: the second run must change nothing
        async with service_session() as s:
            assert await recompute.learn_priors(s) == 1
    async with service_session() as s:
        row = (await s.execute(
            text("SELECT mean_ratio, source, learned_days FROM occupancy_prior "
                 "WHERE venue_id = :v AND learned_days > 0"), {"v": venue_id})).mappings().one()
    assert row["source"] == "learned" and row["learned_days"] == 3
    assert row["mean_ratio"] == pytest.approx(0.3 * 0.5 + 0.9 * 0.5, abs=1e-4)
