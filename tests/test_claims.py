"""Claims, registrations and owner-issued staff devices."""

from __future__ import annotations

import pytest

from haazir.auth.jwt import issue_access

from .conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


def _h(uid, role):
    return {"Authorization": f"Bearer {issue_access(uid, role)}"}


async def test_an_approved_claim_makes_an_owner_who_can_issue_devices(
    client, venue_factory, user_factory
):
    diner_id, _ = await user_factory()
    admin_id, _ = await user_factory(role="admin")
    venue_id = await venue_factory("Claim Me")

    r = await client.post("/v1/owner/claims", json={"venue_id": str(venue_id),
                                                    "proof_note": "I own it"},
                          headers=_h(diner_id, "diner"))
    assert r.status_code == 201, r.text
    claim_id = r.json()["claim_id"]

    pending = (await client.get("/v1/admin/claims", headers=_h(admin_id, "admin"))).json()
    assert [c["claim_id"] for c in pending] == [claim_id]

    r = await client.post(f"/v1/admin/claims/{claim_id}/decide", json={"approve": True},
                          headers=_h(admin_id, "admin"))
    assert r.status_code == 200, r.text

    owner = _h(diner_id, "owner")
    assert [v["id"] for v in (await client.get("/v1/owner/venues", headers=owner)).json()] \
        == [str(venue_id)]
    r = await client.post(f"/v1/owner/venues/{venue_id}/devices", json={"label": "counter"},
                          headers=owner)
    assert r.status_code == 201 and r.json()["token"].startswith("hzd_")
    assert len((await client.get(f"/v1/owner/venues/{venue_id}/devices",
                                 headers=owner)).json()) == 1


async def test_a_registration_becomes_a_venue(client, user_factory):
    uid, _ = await user_factory()
    admin_id, _ = await user_factory(role="admin")
    r = await client.post(
        "/v1/owner/claims",
        json={"new_venue": {"name": "Nayi Biryani", "area_slug": "bahadurabad",
                            "cuisines": ["biryani"], "lat": 24.876, "lng": 67.064}},
        headers=_h(uid, "diner"),
    )
    claim_id = r.json()["claim_id"]
    r = await client.post(f"/v1/admin/claims/{claim_id}/decide", json={"approve": True},
                          headers=_h(admin_id, "admin"))
    assert r.status_code == 200, r.text
    card = (await client.get(f"/v1/venues/{r.json()['venue_id']}")).json()
    assert card["name"] == "Nayi Biryani"


async def test_someone_elses_venue_is_not_yours_to_issue_for(client, venue_factory, user_factory):
    owner_id, _ = await user_factory(role="owner")
    venue_id = await venue_factory()
    r = await client.post(f"/v1/owner/venues/{venue_id}/devices", json={},
                          headers=_h(owner_id, "owner"))
    assert r.status_code == 404
