"""Staff device tokens and the geofence. Plan §5.

Phase 2's acceptance criterion: *a staff POST from outside the geofence returns 409*. The
distance is computed by PostGIS on the geography type, so these coordinates are real metres
rather than degrees pretending to be metres.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from haazir.auth import device as device_auth
from haazir.db import service_session

from .conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

# Burns Road, and a point about 1.1 km north of it.
BURNS_ROAD = (24.8615, 67.0180)
FAR_AWAY = (24.8715, 67.0180)


async def _device_for(venue_id: uuid.UUID, geofence_m: int = 300) -> str:
    async with service_session() as s:
        token, _ = await device_auth.issue(s, venue_id, label="counter tablet",
                                           geofence_m=geofence_m)
    return token


async def test_a_device_token_authenticates_and_is_scoped_to_one_venue(clean_db, venue_factory):
    venue_id = await venue_factory("Kolachi", *BURNS_ROAD)
    token = await _device_for(venue_id)
    async with service_session() as s:
        device = await device_auth.authenticate(s, token)
    assert device.venue_id == venue_id
    assert device.geofence_m == 300


async def test_a_revoked_device_stops_working(clean_db, venue_factory):
    venue_id = await venue_factory()
    token = await _device_for(venue_id)
    async with service_session() as s:
        device = await device_auth.authenticate(s, token)
        assert await device_auth.revoke(s, device.id)
    async with service_session() as s:
        with pytest.raises(device_auth.DeviceInvalid):
            await device_auth.authenticate(s, token)


async def test_last_used_at_is_updated_on_every_call(clean_db, venue_factory):
    """What makes "90 days, renews on use" true, and what lets a device that has gone quiet
    be spotted."""
    venue_id = await venue_factory()
    token = await _device_for(venue_id)
    async with service_session() as s:
        await device_auth.authenticate(s, token)
    async with service_session() as s:
        used = await s.scalar(
            text("SELECT last_used_at FROM device_token WHERE venue_id = :v"), {"v": venue_id}
        )
    assert used is not None


async def test_the_raw_token_is_never_stored(clean_db, venue_factory):
    venue_id = await venue_factory()
    token = await _device_for(venue_id)
    async with service_session() as s:
        stored = await s.scalar(
            text("SELECT token_hash FROM device_token WHERE venue_id = :v"), {"v": venue_id}
        )
    assert stored != token
    assert token not in stored


# --- the geofence ------------------------------------------------------------


async def test_a_post_from_inside_the_geofence_is_accepted(client, clean_db, venue_factory):
    venue_id = await venue_factory("Kolachi", *BURNS_ROAD)
    token = await _device_for(venue_id)
    r = await client.post(
        "/v1/staff/state",
        json={"band": "busy", "wait_min": 25, "lat": BURNS_ROAD[0], "lng": BURNS_ROAD[1]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["geo_ok"] is True
    assert body["distance_m"] < 300


async def test_a_post_from_outside_the_geofence_returns_409(client, clean_db, venue_factory):
    venue_id = await venue_factory("Kolachi", *BURNS_ROAD)
    token = await _device_for(venue_id)
    r = await client.post(
        "/v1/staff/state",
        json={"band": "free", "lat": FAR_AWAY[0], "lng": FAR_AWAY[1]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409


async def test_a_rejected_post_is_still_recorded(client, clean_db, venue_factory):
    """§14 rule 4 and the estimator both need the attempt to exist. Silently dropping it
    would mean someone probing the fence leaves no trace."""
    venue_id = await venue_factory("Kolachi", *BURNS_ROAD)
    token = await _device_for(venue_id)
    await client.post(
        "/v1/staff/state",
        json={"band": "free", "lat": FAR_AWAY[0], "lng": FAR_AWAY[1]},
        headers={"Authorization": f"Bearer {token}"},
    )
    async with service_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT geo_ok, reporter_trust FROM observation "
                    "WHERE venue_id = :v ORDER BY observed_at DESC LIMIT 1"
                ),
                {"v": venue_id},
            )
        ).mappings().first()
    assert row is not None
    assert row["geo_ok"] is False
    assert row["reporter_trust"] < 0.1  # kept, and weighted to almost nothing


async def test_a_post_with_no_position_fails_the_check(client, clean_db, venue_factory):
    venue_id = await venue_factory()
    token = await _device_for(venue_id)
    r = await client.post(
        "/v1/staff/state",
        json={"band": "full"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409


# --- RLS on observations -----------------------------------------------------


async def test_a_device_cannot_post_for_another_venue(clean_db, venue_factory):
    """RLS, not the endpoint, is what enforces this. The endpoint reads `venue_id` from the
    token, so this test goes under it and writes the row directly."""
    from sqlalchemy.exc import DBAPIError

    from haazir.db import Claims, session_scope

    mine = await venue_factory("Mine", *BURNS_ROAD)
    theirs = await venue_factory("Theirs", *BURNS_ROAD)

    with pytest.raises(DBAPIError):
        async with session_scope(Claims(role="staff", venue_id=mine)) as s:
            await s.execute(
                text(
                    "INSERT INTO observation (venue_id, source, observed_at, value, sigma) "
                    "VALUES (:v, 'staff', now(), 0.9, 0.09)"
                ),
                {"v": theirs},
            )


async def test_nobody_can_read_raw_observations(clean_db, venue_factory, user_factory):
    """§4: the public sees `live_state` and aggregates. Raw observations are the estimator's
    input and have no user-facing read path at all."""
    from haazir.db import Claims, session_scope

    venue_id = await venue_factory(*("Kolachi", *BURNS_ROAD))
    async with service_session() as s:
        await s.execute(
            text(
                "INSERT INTO observation (venue_id, source, observed_at, value, sigma) "
                "VALUES (:v, 'staff', now(), 0.9, 0.09)"
            ),
            {"v": venue_id},
        )

    user_id, _ = await user_factory()
    for claims in (
        Claims(),
        Claims(role="diner", user_id=user_id),
        Claims(role="staff", venue_id=venue_id),
    ):
        async with session_scope(claims) as s:
            n = await s.scalar(
                text("SELECT count(*) FROM observation WHERE venue_id = :v"), {"v": venue_id}
            )
            assert n == 0, f"{claims.role} could read raw observations"


async def test_a_user_access_token_cannot_post_staff_state(client, clean_db, user_factory):
    from haazir.auth.jwt import issue_access

    user_id, _ = await user_factory()
    access = issue_access(user_id, "diner")
    r = await client.post(
        "/v1/staff/state",
        json={"band": "full", "lat": BURNS_ROAD[0], "lng": BURNS_ROAD[1]},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert r.status_code == 403
