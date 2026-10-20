"""Cameras end to end: register, zones, ingest into the fusion, the public table map."""

from __future__ import annotations

import pytest

from haazir.auth.jwt import issue_access
from haazir.services import realtime

from .conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

ZONES = {
    "frame_w": 640, "frame_h": 480,
    "zones": [
        {"table_id": f"t{i}", "label": f"T{i}", "seats": 4,
         "polygon": [[0, 0], [10, 0], [10, 10]]}
        for i in range(1, 5)
    ],
}


async def test_a_camera_reading_moves_the_live_state_and_the_table_map(
    client, venue_factory, user_factory
):
    owner_id, _ = await user_factory(role="owner")
    venue_id = await venue_factory("Camera Cafe", claimed_by=owner_id)
    owner = {"Authorization": f"Bearer {issue_access(owner_id, 'owner')}"}

    r = await client.post(f"/v1/owner/venues/{venue_id}/cameras",
                          json={"name": "Hall", "kind": "webcam"}, headers=owner)
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    assert token.startswith("hzc_")
    cam = {"Authorization": f"Bearer {token}"}

    assert (await client.put("/v1/camera/zones", json=ZONES, headers=cam)).status_code == 200
    assert len((await client.get("/v1/camera/zones", headers=cam)).json()["zones"]) == 4

    r = await client.post(
        "/v1/camera/ingest",
        json={"people": 9, "fps": 4.0,
              "tables": [{"table_id": "t1", "occupied": True, "persons": 4},
                         {"table_id": "t2", "occupied": True, "persons": 3},
                         {"table_id": "t3", "occupied": True, "persons": 2}]},
        headers=cam,
    )
    assert r.status_code == 202, r.text
    assert r.json()["occupancy"] == 0.75

    # Rate limited: a second reading inside three seconds.
    assert (await client.post("/v1/camera/ingest", json={"tables": []},
                              headers=cam)).status_code == 429

    tables = (await client.get(f"/v1/venues/{venue_id}/tables")).json()
    assert tables["source"] == "camera"
    assert [t["occupied"] for t in tables["tables"]] == [True, True, True, False]
    assert tables["people"] == 9

    card = (await client.get(f"/v1/venues/{venue_id}")).json()
    assert "camera" in card["live"]["source_weights"]
    kinds = [e.kind for e in realtime.hub.missed_since(venue_id, 0)]
    assert "live" in kinds and "tables" in kinds

    listed = (await client.get(f"/v1/owner/venues/{venue_id}/cameras", headers=owner)).json()
    assert listed[0]["status"] == "online" and listed[0]["tables_occupied"] == 3


async def test_cameras_are_scoped_to_the_venue_owner(client, venue_factory, user_factory):
    owner_id, _ = await user_factory(role="owner")
    other_id, _ = await user_factory(role="owner")
    venue_id = await venue_factory(claimed_by=owner_id)
    other = {"Authorization": f"Bearer {issue_access(other_id, 'owner')}"}
    r = await client.post(f"/v1/owner/venues/{venue_id}/cameras",
                          json={"name": "x"}, headers=other)
    assert r.status_code == 404


async def test_a_bad_camera_token_is_refused(client):
    r = await client.get("/v1/camera/zones", headers={"Authorization": "Bearer hzc_nope"})
    assert r.status_code == 401


async def test_a_venue_without_a_camera_gets_an_estimated_map(client, venue_factory):
    venue_id = await venue_factory()
    r = await client.get(f"/v1/venues/{venue_id}/tables")
    assert r.status_code == 200
    assert r.json()["source"] == "estimate" and r.json()["tables"]
