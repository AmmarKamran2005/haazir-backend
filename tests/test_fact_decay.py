"""The nightly fact decay. §3.4.

The first version multiplied the already-decayed confidence every night, which compounds. It
took a scraped fact below the 0.5 hard-filter floor in about twelve nights instead of about
seventy-three, and the access filters (`needs_card`, `needs_ramp`) returned nothing in
production without any test noticing, because nothing tested the job at all.

These pin the property that matters: the result depends on the fact's age and nothing else,
so running the job again changes nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import math

import pytest
from sqlalchemy import text

from haazir.db import service_session
from haazir.services import ingest_venues as ingest
from haazir.services import recompute

from .conftest import requires_db
from .test_ingest import venue_record

pytestmark = [pytest.mark.asyncio, requires_db]

PLACE = "test-place-1"


def _days_ago(n: int) -> str:
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


async def _seed(s, facts: dict) -> None:
    await ingest.load_venues(s, [venue_record()])
    await s.execute(
        text("UPDATE venue SET attributes = CAST(:a AS jsonb) WHERE place_id = :p"),
        {"a": json.dumps(facts), "p": PLACE},
    )


async def _attrs(s) -> dict:
    return await s.scalar(text("SELECT attributes FROM venue WHERE place_id = :p"), {"p": PLACE})


async def test_decay_follows_the_age_of_the_fact(clean_db):
    async with service_session() as s:
        await _seed(s, {"dine_in": {"v": True, "c": 0.75, "c0": 0.75, "n": 0,
                                    "at": _days_ago(90), "src": "places_api"}})
        await recompute.decay_facts(s)
        c = (await _attrs(s))["dine_in"]["c"]
    assert c == pytest.approx(0.75 * math.exp(-90 / 180), abs=1e-6)


async def test_running_it_again_changes_nothing(clean_db):
    """The regression. Thirty runs must give what one run gives."""
    async with service_session() as s:
        await _seed(s, {"dine_in": {"v": True, "c": 0.75, "c0": 0.75, "n": 0,
                                    "at": _days_ago(29), "src": "places_api"}})
        await recompute.decay_facts(s)
        once = (await _attrs(s))["dine_in"]["c"]
        for _ in range(29):
            await recompute.decay_facts(s)
        many = (await _attrs(s))["dine_in"]["c"]
    assert many == once
    # And it is still above the floor the hard filters use, which the compounding version
    # had pushed to 0.25 by now.
    assert once == pytest.approx(0.75 * math.exp(-29 / 180), abs=1e-6)
    assert once > 0.5


async def test_a_fact_that_predates_c0_uses_its_current_confidence_once(clean_db):
    async with service_session() as s:
        await _seed(s, {"wifi": {"v": True, "c": 0.6, "n": 3, "at": _days_ago(0),
                                 "src": "diner_verified"}})
        await recompute.decay_facts(s)
        first = (await _attrs(s))["wifi"]
        await recompute.decay_facts(s)
        second = (await _attrs(s))["wifi"]
    assert first["c0"] == pytest.approx(0.6)
    assert first["c"] == pytest.approx(0.6)  # verified today: no decay yet
    assert second == first


async def test_the_floor_holds_and_never_raises_a_fact(clean_db):
    async with service_session() as s:
        await _seed(
            s,
            {
                "old": {"v": True, "c": 0.75, "c0": 0.75, "n": 0, "at": _days_ago(2000),
                        "src": "places_api"},
                "doubtful": {"v": True, "c": 0.1, "c0": 0.1, "n": 1, "at": _days_ago(500),
                             "src": "diner_verified"},
            },
        )
        await recompute.decay_facts(s)
        attrs = await _attrs(s)
    assert attrs["old"]["c"] == pytest.approx(0.25)
    # A fact recorded below the floor stays where it was; the floor is a floor on decay,
    # not a promotion.
    assert attrs["doubtful"]["c"] == pytest.approx(0.1)


async def test_verified_value_and_other_fields_are_untouched(clean_db):
    async with service_session() as s:
        await _seed(s, {"dine_in": {"v": False, "c": 0.75, "c0": 0.75, "n": 4,
                                    "at": _days_ago(10), "src": "diner_verified"}})
        await recompute.decay_facts(s)
        fact = (await _attrs(s))["dine_in"]
    assert fact["v"] is False
    assert fact["n"] == 4
    assert fact["src"] == "diner_verified"
    assert fact["at"] == _days_ago(10)


async def test_a_new_scrape_records_c0_alongside_c(clean_db):
    async with service_session() as s:
        await ingest.load_venues(s, [venue_record()])
        attrs = await _attrs(s)
    assert attrs["dine_in"]["c0"] == attrs["dine_in"]["c"] == 0.75
