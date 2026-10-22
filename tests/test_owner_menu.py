"""An owner's menu line and offer reach customers: venue menu, compare, card."""

from __future__ import annotations

import pytest

from haazir.auth.jwt import issue_access

from .conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


async def test_an_owner_dish_and_offer_are_public(client, venue_factory, user_factory):
    owner_id, _ = await user_factory(role="owner")
    venue_id = await venue_factory("Kababjees Bakers", claimed_by=owner_id)
    owner = {"Authorization": f"Bearer {issue_access(owner_id, 'owner')}"}

    r = await client.post(f"/v1/owner/venues/{venue_id}/dishes",
                          json={"name": "Lotus cake 1 pound", "section": "Cakes",
                                "price_pkr": 2400}, headers=owner)
    assert r.status_code == 201, r.text
    dish = r.json()
    assert dish["portion"] == "1 pound" and dish["price_pkr"] == 2400
    # A second size of the same cake is its own line, not an overwrite.
    r = await client.post(f"/v1/owner/venues/{venue_id}/dishes",
                          json={"name": "Lotus cake 2 pound", "price_pkr": 4500}, headers=owner)
    assert r.status_code == 201, r.text

    menu = (await client.get(f"/v1/venues/{venue_id}/dishes")).json()["dishes"]
    assert {(d["menu_name"], d["portion"]) for d in menu} >= {("Lotus cake 1 pound", "1 pound"),
                                                             ("Lotus cake 2 pound", "2 pound")}

    r = await client.patch(f"/v1/owner/venues/{venue_id}/dishes/{dish['dish_id']}",
                           json={"price_pkr": 2500, "sold_out": True}, headers=owner)
    assert r.json()["price_pkr"] == 2500 and r.json()["sold_out"] is True

    cmp = (await client.get("/v1/dishes/compare",
                            params={"dish": "lotus cake", "venues": str(venue_id)})).json()
    assert cmp["count"] == 2 and cmp["results"][0]["portion"] == "1 pound"
    prices = (await client.get("/v1/dishes/lotus cake/prices",
                               params={"venues": str(venue_id)})).json()
    assert {q["portion"] for q in prices["quotes"]} == {"1 pound", "2 pound"}
    near = (await client.get("/v1/dishes/compare",
                             params={"dish": "lotus cake", "lat": 24.8615, "lng": 67.0180})).json()
    assert near["results"][0]["distance_m"] is not None

    r = await client.post(f"/v1/owner/venues/{venue_id}/offers",
                          json={"title": "Chai time", "discount_pct": 15, "days": [1, 2, 3],
                                "start_hour": 15, "end_hour": 18}, headers=owner)
    assert r.status_code == 201, r.text
    card = (await client.get(f"/v1/venues/{venue_id}")).json()
    assert card["offers"][0]["title"] == "Chai time" and card["offers"][0]["days"] == [1, 2, 3]

    r = await client.delete(f"/v1/owner/venues/{venue_id}/dishes/{dish['dish_id']}",
                            headers=owner)
    assert r.status_code == 204
