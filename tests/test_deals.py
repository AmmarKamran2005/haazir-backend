"""Card deals: venue and chain matching, filters, and the deals on the venue card."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from haazir.db import service_session
from haazir.services.deals import canonical_bank

from .conftest import requires_db


def test_bank_names_are_canonicalised():
    assert canonical_bank("hbl") == "HBL"
    assert canonical_bank("Meezan Bank") == "Meezan Bank"
    assert canonical_bank("meezan") == "Meezan Bank"
    assert canonical_bank(None) is None


@pytest.mark.asyncio
@requires_db
async def test_venue_and_chain_deals_reach_the_card_and_the_list(client, venue_factory):
    solo = await venue_factory("Javed Nihari")
    branch = await venue_factory("Ginsoy Gulshan Branch")
    async with service_session() as s:
        await s.execute(
            text(
                "INSERT INTO card_deal (bank, venue_id, discount_pct, max_discount_pkr, is_demo) "
                "VALUES ('HBL', :v, 15, 1000, TRUE)"
            ),
            {"v": solo},
        )
        await s.execute(
            text("INSERT INTO card_deal (bank, chain_name, discount_pct) "
                 "VALUES ('UBL', 'Ginsoy', 25)")
        )

    card = (await client.get(f"/v1/venues/{solo}")).json()
    assert card["deals"][0]["bank"] == "HBL" and card["deals"][0]["cap"] == 1000
    card = (await client.get(f"/v1/venues/{branch}")).json()
    assert card["deals"][0]["discount_pct"] == 25

    hbl = (await client.get("/v1/deals", params={"bank": "hbl"})).json()
    assert [d["venue"]["id"] for d in hbl] == [str(solo)]
    assert "is_demo" not in hbl[0]
