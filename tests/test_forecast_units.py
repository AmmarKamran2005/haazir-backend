"""The forecast's arithmetic."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from haazir.estimator import forecast

KHI = ZoneInfo("Asia/Karachi")
# Friday 20:10 Karachi. Friday is weekday 4, stored day 5.
NOW = dt.datetime(2026, 10, 23, 20, 10, tzinfo=KHI)
FRI_8PM = 5 * 24 + 20


def flat(value: float) -> dict[int, float]:
    return {h: value for h in range(168)}


def test_steps_are_on_the_half_hour_and_cover_the_window():
    out = forecast.project(flat(0.4), None, 3, 80, now=NOW)
    times = [dt.datetime.fromisoformat(p["at"]) for p in out["points"]]
    assert times[0].minute == 30 and times[0].hour == 20
    assert all(t.minute in (0, 30) for t in times)
    assert len(times) == 6


def test_without_a_live_reading_it_is_the_weekly_pattern():
    out = forecast.project(flat(0.4), None, 2, 80, now=NOW)
    assert out["now"]["occupancy"] == 0.4
    assert {p["occupancy"] for p in out["points"]} == {0.4}


def test_a_live_deviation_fades_back_into_the_pattern():
    out = forecast.project(flat(0.4), 0.95, 6, 80, now=NOW)
    occ = [p["occupancy"] for p in out["points"]]
    assert out["now"]["band"] == "full"
    assert occ == sorted(occ, reverse=True)
    assert occ[0] > 0.7 and abs(occ[-1] - 0.4) < 0.01
    assert out["points"][0]["wait_min"] > out["points"][-1]["wait_min"]


def test_the_prior_is_read_at_the_venue_hour():
    prior = flat(0.2)
    prior[FRI_8PM] = 0.9
    prior[FRI_8PM + 1] = 0.9
    out = forecast.project(prior, None, 1, 80, now=NOW)
    assert out["now"]["occupancy"] > 0.85
