"""The partner dashboard. Plan §12 Phase 7 (the slice the demo needs).

*"An owner cannot modify `tier`, `claimed_by`, `google_rating` or `trust_score` through any
endpoint; the price-position endpoint returns the area median correctly."*

The first half is already proved at the database in `test_rls_venue.py`, which writes straight
to the table and watches the trigger reset the protected columns. What this file adds is the
API side: that the endpoint's own allow-list agrees, and — more usefully — that a field
outside it is rejected rather than silently ignored.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from haazir.auth.jwt import issue_access
from haazir.db import service_session
from haazir.services import ingest_venues as ingest

from .conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

BURNS = (24.8615, 67.0180)


def venue(place_id: str, name: str, **over) -> dict:
    return {
        "place_id": place_id, "name": name, "area": "Burns Road",
        "lat": BURNS[0], "lng": BURNS[1], "venue_type": "restaurant",
        "cuisines": ["Pakistani", "BBQ"], "google_rating": 4.4,
        "google_review_count": 800, "attributes": {"dine_in": True},
        "scraped_at": "2026-09-03T12:00:00Z",
    } | over


@pytest.fixture
async def owned(clean_db, user_factory):
    """One claimed venue and three competitors pricing the same dish."""
    owner_id, _ = await user_factory(role="owner")

    async with service_session() as s:
        await ingest.load_venues(
            s,
            [
                venue("mine", "My Restaurant", avg_ticket_pkr=1200),
                venue("rival1", "Rival One"),
                venue("rival2", "Rival Two"),
                venue("rival3", "Rival Three"),
                venue("other-area", "Far Away Rival", lat=25.0100, lng=67.3200,
                      area="Bahria Town"),
            ],
        )
        await ingest.load_menu_items(
            s,
            [
                {"place_id": "mine", "name": "Malai Boti", "price_pkr": 1250},
                {"place_id": "rival1", "name": "Malai Boti", "price_pkr": 900},
                {"place_id": "rival2", "name": "Malai Tikka Boti", "price_pkr": 980},
                {"place_id": "rival3", "name": "Malai Boti", "price_pkr": 1050},
                # Same dish, different area: must not enter the local median.
                {"place_id": "other-area", "name": "Malai Boti", "price_pkr": 4000},
                # A protein variant: must not be averaged with the plain one.
                {"place_id": "rival1", "name": "Beef Malai Boti", "price_pkr": 1800},
                # Only this venue prices it, so it is not comparable.
                {"place_id": "mine", "name": "Kaleji Masala", "price_pkr": 700},
            ],
        )
        await s.execute(
            text("UPDATE venue SET claimed_by = :u, tier = 'claimed' WHERE place_id = 'mine'"),
            {"u": owner_id},
        )
        venue_id = await s.scalar(text("SELECT id FROM venue WHERE place_id = 'mine'"))

    return {
        "owner_id": owner_id,
        "venue_id": venue_id,
        "headers": {"Authorization": f"Bearer {issue_access(owner_id, 'owner')}"},
    }


# --- the screen that brings an owner back ------------------------------------


async def test_price_position_reports_the_area_median(client, owned):
    """Phase 7's second criterion. Rivals at 900, 980 and 1050; the median is 980."""
    body = (
        await client.get(f"/v1/owner/venues/{owned['venue_id']}/prices",
                         headers=owned["headers"])
    ).json()

    malai = next(d for d in body["dishes"] if d["family"] == "malai boti"
                 and d["protein"] is None)
    assert malai["your_price_pkr"] == 1250
    assert malai["area_median_pkr"] == 980
    assert malai["delta_pkr"] == 270
    assert malai["delta_pct"] == pytest.approx(27.6, abs=0.5)
    assert malai["comparable_venues"] == 3
    assert malai["comparable"] is True


async def test_a_different_area_is_not_in_the_median(client, owned):
    """The Rs 4,000 listing in Bahria Town would drag the median up by a third."""
    body = (
        await client.get(f"/v1/owner/venues/{owned['venue_id']}/prices",
                         headers=owned["headers"])
    ).json()
    malai = next(d for d in body["dishes"] if d["family"] == "malai boti"
                 and d["protein"] is None)
    assert malai["area_max_pkr"] < 4000


async def test_beef_is_never_compared_against_plain(client, owned):
    """The whole reason `dish.protein` exists. Averaging them is a confidently wrong answer
    that would tell this owner to cut a price that is not high."""
    body = (
        await client.get(f"/v1/owner/venues/{owned['venue_id']}/prices",
                         headers=owned["headers"])
    ).json()
    malai = next(d for d in body["dishes"] if d["family"] == "malai boti"
                 and d["protein"] is None)
    assert malai["area_max_pkr"] <= 1050  # the Rs 1,800 beef variant is not in this bucket


async def test_a_dish_nobody_else_prices_is_marked_not_comparable(client, owned):
    """A median of nothing must not be presented as a benchmark to move a price against."""
    body = (
        await client.get(f"/v1/owner/venues/{owned['venue_id']}/prices",
                         headers=owned["headers"])
    ).json()
    kaleji = next(d for d in body["dishes"] if d["family"] == "kaleji masala")
    assert kaleji["comparable"] is False
    assert kaleji["comparable_venues"] == 0
    assert body["comparable_dishes"] < body["priced_dishes"]


# --- what an owner may and may not change ------------------------------------


async def test_an_owner_can_correct_the_operational_facts(client, owned):
    r = await client.patch(
        f"/v1/owner/venues/{owned['venue_id']}",
        json={"capacity_covers": 140, "phone": "021-111-529-233"},
        headers=owned["headers"],
    )
    assert r.status_code == 200

    async with service_session() as s:
        row = (
            await s.execute(
                text("SELECT capacity_covers, phone FROM venue WHERE id = :v"),
                {"v": owned["venue_id"]},
            )
        ).mappings().one()
    assert row["capacity_covers"] == 140
    assert row["phone"] == "+9221111529233"  # normalised to E.164 on the way in


@pytest.mark.parametrize(
    "forbidden",
    [{"tier": "live"}, {"claimed_by": "00000000-0000-0000-0000-000000000000"},
     {"google_rating": 5.0}, {"trust_score": 100}, {"status": "hidden"}],
)
async def test_a_field_outside_the_allow_list_is_refused_not_ignored(client, owned, forbidden):
    """Rejecting is better than dropping it. An owner who sends `tier` and gets a 200 back
    reasonably believes it worked."""
    r = await client.patch(
        f"/v1/owner/venues/{owned['venue_id']}", json=forbidden, headers=owned["headers"]
    )
    assert r.status_code == 422


async def test_the_protected_columns_survive_even_a_direct_write(owned):
    """The guarantee the endpoint cannot make on its own. Migration 0010's trigger is what
    covers endpoints that do not exist yet."""
    from haazir.db import Claims, session_scope

    async with session_scope(Claims(role="owner", user_id=owned["owner_id"])) as s:
        await s.execute(
            text("UPDATE venue SET tier = 'live', google_rating = 5.0 WHERE id = :v"),
            {"v": owned["venue_id"]},
        )
    async with service_session() as s:
        row = (
            await s.execute(
                text("SELECT tier, google_rating FROM venue WHERE id = :v"),
                {"v": owned["venue_id"]},
            )
        ).mappings().one()
    assert row["tier"] == "claimed"
    assert row["google_rating"] == pytest.approx(4.4)


async def test_editing_records_that_an_owner_touched_the_venue(client, owned):
    """§10.1 rule 6: the next scrape must not overwrite what the owner just told us."""
    await client.patch(
        f"/v1/owner/venues/{owned['venue_id']}", json={"capacity_covers": 90},
        headers=owned["headers"],
    )
    async with service_session() as s:
        sources = [
            r[0]
            for r in await s.execute(
                text("SELECT source FROM venue_source WHERE venue_id = :v"),
                {"v": owned["venue_id"]},
            )
        ]
    assert "owner" in sources


async def test_an_unrecognisable_phone_is_rejected(client, owned):
    r = await client.patch(
        f"/v1/owner/venues/{owned['venue_id']}", json={"phone": "12"},
        headers=owned["headers"],
    )
    assert r.status_code == 400


# --- ownership boundaries ----------------------------------------------------


async def test_another_owners_venue_is_404_not_403(client, owned, user_factory):
    """403 would confirm the venue exists and is claimed, which turns this endpoint into a
    way to enumerate which venues have owners."""
    other_id, _ = await user_factory(role="owner")
    headers = {"Authorization": f"Bearer {issue_access(other_id, 'owner')}"}

    for path in ("", "/prices", "/analytics", "/attribution"):
        r = await client.get(f"/v1/owner/venues/{owned['venue_id']}{path}", headers=headers) \
            if path else await client.patch(
                f"/v1/owner/venues/{owned['venue_id']}",
                json={"capacity_covers": 10}, headers=headers)
        assert r.status_code == 404, path


async def test_my_venues_lists_only_what_this_owner_claimed(client, owned, user_factory):
    body = (await client.get("/v1/owner/venues", headers=owned["headers"])).json()
    assert [v["name"] for v in body] == ["My Restaurant"]

    other_id, _ = await user_factory(role="owner")
    empty = (
        await client.get(
            "/v1/owner/venues",
            headers={"Authorization": f"Bearer {issue_access(other_id, 'owner')}"},
        )
    ).json()
    assert empty == []


async def test_the_owner_surface_requires_a_login(client, owned):
    assert (await client.get("/v1/owner/venues")).status_code == 401
    assert (
        await client.get(f"/v1/owner/venues/{owned['venue_id']}/prices")
    ).status_code == 401


# --- analytics ---------------------------------------------------------------


async def test_analytics_returns_the_full_week_and_says_how_much_is_measured(client, owned):
    body = (
        await client.get(f"/v1/owner/venues/{owned['venue_id']}/analytics",
                         headers=owned["headers"])
    ).json()

    assert len(body["grid"]) == 168
    assert {g["weekday"] for g in body["grid"]} == set(range(7))
    assert body["observed_hours"] == 0
    # An owner reading a heatmap should know whether it is their restaurant or a category
    # average, and the response says which without being asked.
    assert "archetype" in body["note"] or "popular times" in body["note"]


async def test_quiet_windows_avoid_the_middle_of_the_night(client, owned):
    """A 4am trough is not an opportunity to discount into."""
    body = (
        await client.get(f"/v1/owner/venues/{owned['venue_id']}/analytics",
                         headers=owned["headers"])
    ).json()
    assert body["quiet_windows"]
    assert all(11 <= w["hour"] <= 23 for w in body["quiet_windows"])


async def test_attribution_never_folds_unverified_into_the_total(client, owned, user_factory):
    """A number that flatters us is worth nothing to the person deciding whether to pay."""
    diner_id, _ = await user_factory()
    await client.post(
        f"/v1/venues/{owned['venue_id']}/hold",
        json={"party_size": 3},
        headers={"Authorization": f"Bearer {issue_access(diner_id, 'diner')}"},
    )

    body = (
        await client.get(f"/v1/owner/venues/{owned['venue_id']}/attribution",
                         headers=owned["headers"])
    ).json()
    assert body["referred_total"] == 1
    assert body["seated"] == 0
    assert body["receipt_verified"] == 0
    assert body["verified_revenue_pkr"] == 0
    assert "never folded" in body["note"]
