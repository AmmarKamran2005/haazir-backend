"""Phase 5 acceptance: the live loop. Plan §12, §8.

*"A staff POST reaches a subscribed SSE client in under 500 ms; confidence and weights
visibly change; killing and reconnecting the SSE client resumes without duplicate events."*

The third clause is the one worth writing carefully. A reconnect that replays an event the
client already rendered, or skips one it did not, both look on screen like the estimate
flickering, and neither raises anything anywhere.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from haazir.auth import device as device_auth
from haazir.db import service_session
from haazir.services import realtime
from haazir.services.realtime import Hub

from .conftest import requires_db

BURNS = (24.8615, 67.0180)


# --- the hub, no database ----------------------------------------------------


def test_a_subscriber_receives_a_published_event():
    hub = Hub()
    venue = uuid.uuid4()

    async def run():
        async with hub.subscribe(venue) as queue:
            hub.publish(venue, "live", {"occupancy": 0.8})
            return await asyncio.wait_for(queue.get(), timeout=1)

    event = asyncio.run(run())
    assert event.kind == "live"
    assert event.data["occupancy"] == 0.8


def test_events_only_reach_subscribers_of_that_venue():
    hub = Hub()
    mine, theirs = uuid.uuid4(), uuid.uuid4()

    async def run():
        async with hub.subscribe(mine) as queue:
            hub.publish(theirs, "live", {"occupancy": 0.9})
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(queue.get(), timeout=0.2)

    asyncio.run(run())


def test_a_subscriber_is_forgotten_when_it_leaves():
    hub = Hub()
    venue = uuid.uuid4()

    async def run():
        async with hub.subscribe(venue):
            assert hub.subscriber_count(venue) == 1
        assert hub.subscriber_count(venue) == 0

    asyncio.run(run())


def test_a_slow_client_is_dropped_rather_than_buffered():
    """An unbounded queue in a fan-out turns one stalled mobile connection into the server's
    memory problem."""
    hub = Hub()
    venue = uuid.uuid4()

    async def run():
        async with hub.subscribe(venue):
            for i in range(realtime.QUEUE_MAX + 5):
                hub.publish(venue, "live", {"n": i})
            return hub.subscriber_count(venue)

    assert asyncio.run(run()) == 0


def test_publishing_to_nobody_is_not_an_error():
    """A publish happens inside a request that has already done the thing it announces. A
    failure here must not turn a successful staff tap into a 500."""
    hub = Hub()
    event = hub.publish(uuid.uuid4(), "live", {"occupancy": 0.5})
    assert event.id > 0


# --- reconnect ---------------------------------------------------------------


def test_a_reconnect_replays_only_what_was_missed():
    """The acceptance criterion's third clause."""
    hub = Hub()
    venue = uuid.uuid4()

    first = hub.publish(venue, "live", {"n": 1})
    second = hub.publish(venue, "live", {"n": 2})
    third = hub.publish(venue, "live", {"n": 3})

    missed = hub.missed_since(venue, first.id)
    assert [e.data["n"] for e in missed] == [2, 3]
    assert second.id in {e.id for e in missed}
    assert third.id in {e.id for e in missed}


def test_a_client_that_saw_everything_replays_nothing():
    hub = Hub()
    venue = uuid.uuid4()
    latest = hub.publish(venue, "live", {"n": 1})
    assert hub.missed_since(venue, latest.id) == []


def test_a_first_connection_gets_no_backlog():
    """Without a Last-Event-ID there is nothing to resume from, and replaying history to a
    fresh client would show it state changes that already happened."""
    hub = Hub()
    venue = uuid.uuid4()
    hub.publish(venue, "live", {"n": 1})
    assert hub.missed_since(venue, None) == []


def test_the_backlog_is_bounded():
    hub = Hub()
    venue = uuid.uuid4()
    for i in range(realtime.BACKLOG + 20):
        hub.publish(venue, "live", {"n": i})
    assert len(hub.missed_since(venue, 0)) == realtime.BACKLOG


@pytest.mark.parametrize(
    ("header", "expected"),
    [("7", 7), ("0", 0), (None, None), ("", None), ("abc", None), ("-3", None), (" 12 ", 12)],
)
def test_last_event_id_is_parsed_not_trusted(header, expected):
    assert realtime.parse_last_event_id(header) == expected


def test_event_ids_are_monotonic():
    hub = Hub()
    a, b = uuid.uuid4(), uuid.uuid4()
    ids = [hub.publish(a, "live", {}).id, hub.publish(b, "live", {}).id,
           hub.publish(a, "live", {}).id]
    assert ids == sorted(ids)
    assert len(set(ids)) == 3


def test_an_event_serialises_as_a_valid_sse_frame():
    import json

    hub = Hub()
    venue = uuid.uuid4()
    frame = hub.publish(venue, "live", {"occupancy": 0.81, "band": "busy"}).to_sse()

    lines = frame.strip().split("\n")
    assert lines[0].startswith("id: ")
    assert lines[1] == "event: live"
    payload = json.loads(lines[2].removeprefix("data: "))
    assert payload["occupancy"] == 0.81
    assert "at" in payload
    assert frame.endswith("\n\n")  # the blank line that ends an SSE event


# --- the endpoint and the real loop ------------------------------------------

pytestmark_db = [pytest.mark.asyncio, requires_db]


@pytest.fixture
async def venue_and_device(clean_db):
    from haazir.services import ingest_venues as ingest

    realtime.hub.reset()
    async with service_session() as s:
        await ingest.load_venues(
            s,
            [{
                "place_id": "live-1", "name": "Live Test Kitchen", "area": "Burns Road",
                "lat": BURNS[0], "lng": BURNS[1], "venue_type": "restaurant",
                "cuisines": ["Pakistani"], "attributes": {"dine_in": True},
                "scraped_at": "2026-09-03T12:00:00Z",
            }],
        )
        venue_id = await s.scalar(text("SELECT id FROM venue WHERE place_id = 'live-1'"))
        token, _ = await device_auth.issue(s, venue_id, label="counter tablet")
    return {"venue_id": venue_id, "token": token}


@requires_db
@pytest.mark.asyncio
async def test_a_staff_tap_reaches_a_subscriber(client, venue_and_device):
    """The acceptance criterion's first clause: the tap reaches a subscribed client."""
    venue_id = venue_and_device["venue_id"]

    async with realtime.hub.subscribe(venue_id) as queue:
        response = await client.post(
            "/v1/staff/state",
            json={"band": "full", "wait_min": 35, "lat": BURNS[0], "lng": BURNS[1]},
            headers={"Authorization": f"Bearer {venue_and_device['token']}"},
        )
        assert response.status_code == 200, response.text
        event = await asyncio.wait_for(queue.get(), timeout=5.0)

    assert event.kind == "live"
    assert event.data["band"] in {"busy", "full"}
    assert response.json()["subscribers_notified"] == 1


@requires_db
@pytest.mark.asyncio
async def test_the_tap_path_stays_within_its_round_trip_budget(
    client, venue_and_device, count_round_trips
):
    """The 500 ms half of the criterion, measured in the one unit that travels.

    From Karachi a round trip to Neon in Singapore is about 170 ms; co-located, where the
    plan puts the API, it is about 1 ms. A wall-clock assertion would therefore pass or fail
    on where the test runs rather than on the code. What does not move is the number of
    statements the path issues, and that is what regresses when somebody adds a query to it.

    Sixteen round trips is roughly 16 ms in the deployment this ships into, comfortably
    inside 500 ms, and about 2.7 s from a laptop in Pakistan. Both numbers follow from this
    one; only this one is worth asserting.
    """
    with count_round_trips() as trips:
        response = await client.post(
            "/v1/staff/state",
            json={"band": "full", "wait_min": 35, "lat": BURNS[0], "lng": BURNS[1]},
            headers={"Authorization": f"Bearer {venue_and_device['token']}"},
        )
    assert response.status_code == 200

    assert len(trips) <= 16, (
        f"the staff tap path now issues {len(trips)} statements: "
        + " | ".join(trips.statements)
    )


@requires_db
@pytest.mark.asyncio
async def test_confidence_and_weights_visibly_change(client, venue_and_device):
    """The second clause. A tap must not merely move the number; it must move the reason."""
    venue_id = venue_and_device["venue_id"]
    from haazir.services import recompute

    async with service_session() as s:
        await recompute.refresh_live_state(s, [venue_id])
    before = (await client.get(f"/v1/venues/{venue_id}/live")).json()

    await client.post(
        "/v1/staff/state",
        json={"band": "full", "lat": BURNS[0], "lng": BURNS[1]},
        headers={"Authorization": f"Bearer {venue_and_device['token']}"},
    )
    after = (await client.get(f"/v1/venues/{venue_id}/live")).json()

    assert before["confidence"] == 0.0  # prior alone knows nothing new
    assert after["confidence"] > 0.4
    assert before["is_live"] is False
    assert after["is_live"] is True
    assert [s["source"] for s in before["sources"]] == ["prior"]
    assert "staff" in {s["source"] for s in after["sources"]}

    # Moved *towards* what the staff said, which is not the same as moved up. A "full" tap
    # contributes 0.95, and at the busiest hour of the Karachi week the prior for this venue
    # is higher than that — so a correct fusion pulls the estimate down. An earlier version
    # asserted `after > before` and passed for hours, then failed on a Friday evening: it had
    # encoded "full means higher" rather than "the reading moves toward the evidence", and
    # the hour it broke on is the hour the product matters most.
    from haazir.routers.staff import BAND_VALUE

    target = BAND_VALUE["full"]
    assert abs(after["occupancy"] - target) < abs(before["occupancy"] - target)
    assert after["occupancy"] != before["occupancy"]


@requires_db
@pytest.mark.asyncio
async def test_an_out_of_geofence_tap_publishes_nothing(client, venue_and_device):
    """It is recorded for the audit trail and it must not move anybody's screen."""
    venue_id = venue_and_device["venue_id"]

    async with realtime.hub.subscribe(venue_id) as queue:
        response = await client.post(
            "/v1/staff/state",
            json={"band": "full", "lat": 24.8715, "lng": BURNS[1]},  # ~1.1 km away
            headers={"Authorization": f"Bearer {venue_and_device['token']}"},
        )
        assert response.status_code == 409
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.4)


@requires_db
@pytest.mark.asyncio
async def test_the_stream_endpoint_is_public_and_404s_for_an_unknown_venue(
    client, venue_and_device
):
    assert (await client.get(f"/v1/venues/{uuid.uuid4()}/live/stream")).status_code == 404


@requires_db
@pytest.mark.asyncio
async def test_marking_a_dish_sold_out_publishes_it(client, venue_and_device):
    """"Nihari khatam" is the single most useful thing a venue can say."""
    from haazir.services import ingest_venues as ingest

    venue_id = venue_and_device["venue_id"]
    async with service_session() as s:
        await ingest.load_menu_items(
            s, [{"place_id": "live-1", "name": "Special Nihari", "price_pkr": 450}]
        )
        dish_id = await s.scalar(text("SELECT dish_id FROM venue_dish WHERE venue_id = :v"),
                                 {"v": venue_id})

    async with realtime.hub.subscribe(venue_id) as queue:
        r = await client.post(
            f"/v1/staff/dish/{dish_id}/soldout",
            json={},
            headers={"Authorization": f"Bearer {venue_and_device['token']}"},
        )
        assert r.status_code == 200
        event = await asyncio.wait_for(queue.get(), timeout=2.0)

    assert event.kind == "dish"
    assert event.data["sold_out"] is True

    async with service_session() as s:
        until = await s.scalar(text("SELECT sold_out_until FROM venue_dish WHERE dish_id = :d"),
                               {"d": dish_id})
    assert until is not None


@requires_db
@pytest.mark.asyncio
async def test_a_device_cannot_mark_another_venues_dish(client, venue_and_device):
    assert (
        await client.post(
            f"/v1/staff/dish/{uuid.uuid4()}/soldout",
            json={},
            headers={"Authorization": f"Bearer {venue_and_device['token']}"},
        )
    ).status_code == 404


@requires_db
@pytest.mark.asyncio
async def test_staff_today_returns_what_the_venue_is_paid_in(client, venue_and_device):
    """Without this the console is unpaid data entry, and it stops being used in a fortnight."""
    await client.post(
        "/v1/staff/state",
        json={"band": "busy", "lat": BURNS[0], "lng": BURNS[1]},
        headers={"Authorization": f"Bearer {venue_and_device['token']}"},
    )
    body = (
        await client.get(
            "/v1/staff/today",
            headers={"Authorization": f"Bearer {venue_and_device['token']}"},
        )
    ).json()

    assert body["venue"]["name"] == "Live Test Kitchen"
    assert body["now"]["band"] in {"free", "moderate", "busy", "full"}
    assert body["weekly_mean_utilisation"] is not None
    assert any(h["staff_taps"] > 0 for h in body["hours"])
    assert body["guests_sent_today"] == 0
    # No scraped venue reports capacity, so empty covers is null rather than a scaled guess.
    assert all(h["empty_covers"] is None for h in body["hours"])


@requires_db
@pytest.mark.asyncio
async def test_the_staff_endpoints_reject_a_diner_token(client, venue_and_device, user_factory):
    from haazir.auth.jwt import issue_access

    user_id, _ = await user_factory()
    headers = {"Authorization": f"Bearer {issue_access(user_id, 'diner')}"}
    assert (await client.get("/v1/staff/today", headers=headers)).status_code == 403


# --- diner writes ------------------------------------------------------------


@requires_db
@pytest.mark.asyncio
async def test_a_checkin_at_the_venue_moves_the_estimate(client, venue_and_device, user_factory):
    from haazir.auth.jwt import issue_access

    venue_id = venue_and_device["venue_id"]
    user_id, _ = await user_factory()
    headers = {"Authorization": f"Bearer {issue_access(user_id, 'diner')}"}

    r = await client.post(
        "/v1/checkin",
        json={"venue_id": str(venue_id), "band": "full", "party_size": 4,
              "lat": BURNS[0], "lng": BURNS[1]},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["counted"] is True
    assert body["note"] is None
    assert body["live"]["occupancy"] > 0.5


@requires_db
@pytest.mark.asyncio
async def test_a_checkin_from_far_away_is_kept_and_barely_counted(
    client, venue_and_device, user_factory
):
    """§14 rule 4 and the audit trail: recorded, weighted to almost nothing, and said out
    loud so the reporter learns why nothing moved."""
    from haazir.auth.jwt import issue_access

    venue_id = venue_and_device["venue_id"]
    user_id, _ = await user_factory()

    r = await client.post(
        "/v1/checkin",
        json={"venue_id": str(venue_id), "band": "full", "lat": 24.95, "lng": 67.30},
        headers={"Authorization": f"Bearer {issue_access(user_id, 'diner')}"},
    )
    assert r.status_code == 201
    assert r.json()["counted"] is False
    assert "could not confirm" in r.json()["note"]

    async with service_session() as s:
        row = (
            await s.execute(
                text("SELECT geo_ok, reporter_trust FROM observation "
                     " WHERE venue_id = :v AND source = 'checkin'"),
                {"v": venue_id},
            )
        ).mappings().one()
    assert row["geo_ok"] is False
    assert row["reporter_trust"] < 0.1


@requires_db
@pytest.mark.asyncio
async def test_one_person_cannot_report_the_same_venue_repeatedly(
    client, venue_and_device, user_factory
):
    """Without a cooldown, one phone can pin a restaurant at "full" all evening."""
    from haazir.auth import ratelimit
    from haazir.auth.jwt import issue_access

    ratelimit.reset()
    venue_id = venue_and_device["venue_id"]
    user_id, _ = await user_factory()
    headers = {"Authorization": f"Bearer {issue_access(user_id, 'diner')}"}
    payload = {"venue_id": str(venue_id), "band": "full", "lat": BURNS[0], "lng": BURNS[1]}

    assert (await client.post("/v1/checkin", json=payload, headers=headers)).status_code == 201
    second = await client.post("/v1/checkin", json=payload, headers=headers)
    assert second.status_code == 429
    assert "Retry-After" in second.headers


@requires_db
@pytest.mark.asyncio
async def test_a_hold_creates_an_attribution_and_promises_nothing(
    client, venue_and_device, user_factory
):
    from haazir.auth.jwt import issue_access

    venue_id = venue_and_device["venue_id"]
    user_id, _ = await user_factory()

    r = await client.post(
        f"/v1/venues/{venue_id}/hold",
        json={"party_size": 4, "eta_min": 25},
        headers={"Authorization": f"Bearer {issue_access(user_id, 'diner')}"},
    )
    assert r.status_code == 201
    assert "not a reservation" in r.json()["note"]

    async with service_session() as s:
        n = await s.scalar(
            text("SELECT count(*) FROM attribution WHERE venue_id = :v"), {"v": venue_id}
        )
    assert n == 1


@requires_db
@pytest.mark.asyncio
async def test_verifying_a_fact_raises_its_confidence(client, venue_and_device, user_factory):
    """The only route by which the four facts Google cannot answer stop being null."""
    from haazir.auth.jwt import issue_access

    venue_id = venue_and_device["venue_id"]
    user_id, _ = await user_factory()

    r = await client.post(
        f"/v1/venues/{venue_id}/facts",
        json={"fact_key": "prayer_area", "value": True},
        headers={"Authorization": f"Bearer {issue_access(user_id, 'diner')}"},
    )
    assert r.status_code == 201, r.text
    fact = r.json()["fact"]
    assert fact["v"] is True
    assert fact["n"] == 1
    assert fact["src"] == "diner_verified"

    card = (await client.get(f"/v1/venues/{venue_id}")).json()
    assert card["attributes"]["prayer_area"]["v"] is True


@requires_db
@pytest.mark.asyncio
async def test_agreement_raises_confidence_more_than_a_single_report(
    client, venue_and_device, user_factory
):
    from haazir.auth.jwt import issue_access

    venue_id = venue_and_device["venue_id"]
    confidences = []
    for _ in range(3):
        user_id, _ = await user_factory()
        r = await client.post(
            f"/v1/venues/{venue_id}/facts",
            json={"fact_key": "prayer_area", "value": True},
            headers={"Authorization": f"Bearer {issue_access(user_id, 'diner')}"},
        )
        confidences.append(r.json()["fact"]["c"])
    assert confidences == sorted(confidences)
    assert confidences[-1] > confidences[0]
    assert confidences[-1] < 1.0  # no number of diners makes it a proven thing


@requires_db
@pytest.mark.asyncio
async def test_disagreement_lowers_confidence(client, venue_and_device, user_factory):
    """A fact five people contradict each other about should be less trusted than one nobody
    has touched, not more."""
    from haazir.auth.jwt import issue_access

    venue_id = venue_and_device["venue_id"]
    agreed = None
    for i in range(4):
        user_id, _ = await user_factory()
        r = await client.post(
            f"/v1/venues/{venue_id}/facts",
            json={"fact_key": "wheelchair_accessible", "value": i != 3},
            headers={"Authorization": f"Bearer {issue_access(user_id, 'diner')}"},
        )
        if i == 2:
            agreed = r.json()["fact"]["c"]
    contested = r.json()["fact"]["c"]
    assert contested < agreed


@requires_db
@pytest.mark.asyncio
async def test_a_fact_a_diner_cannot_see_is_refused(client, venue_and_device, user_factory):
    """A crowd vote on kitchen transparency or enforcement history is noise dressed as
    verification."""
    from haazir.auth.jwt import issue_access

    user_id, _ = await user_factory()
    r = await client.post(
        f"/v1/venues/{venue_and_device['venue_id']}/facts",
        json={"fact_key": "kitchen_transparency", "value": True},
        headers={"Authorization": f"Bearer {issue_access(user_id, 'diner')}"},
    )
    assert r.status_code == 400


@requires_db
@pytest.mark.asyncio
async def test_the_diner_writes_require_a_login(client, venue_and_device):
    """Browsing is never gated; contributing is."""
    venue_id = venue_and_device["venue_id"]
    assert (await client.post("/v1/checkin",
                              json={"venue_id": str(venue_id), "band": "full"})).status_code == 401
    assert (await client.post(f"/v1/venues/{venue_id}/hold",
                              json={"party_size": 2})).status_code == 401


# --- trust -------------------------------------------------------------------


@requires_db
@pytest.mark.asyncio
async def test_the_trust_endpoint_shows_only_published_records(client, venue_and_device):
    """§10.2: nothing below 0.90 match confidence is ever visible to a diner."""
    venue_id = venue_and_device["venue_id"]
    async with service_session() as s:
        for confidence, published, reason in (
            (0.97, True, "Published and matched"),
            (0.55, False, "Unreviewed low-confidence match"),
        ):
            await s.execute(
                text(
                    """
                    INSERT INTO regulatory_event
                           (venue_id, authority, event_type, event_date, reason, source_url,
                            source_name, raw_venue_name, match_confidence, published)
                    VALUES (:v, 'Sindh Food Authority', 'sealed', CURRENT_DATE, :reason,
                            'https://example.com/a', 'Dawn', 'Live Test Kitchen', :c, :p)
                    """
                ),
                {"v": venue_id, "c": confidence, "p": published, "reason": reason},
            )

    body = (await client.get(f"/v1/venues/{venue_id}/trust")).json()
    reasons = [e["reason"] for e in body["regulatory"]]
    assert reasons == ["Published and matched"]
    assert all(e["source_url"] for e in body["regulatory"])


@requires_db
@pytest.mark.asyncio
async def test_a_venue_with_no_computed_score_says_so(client, venue_and_device):
    body = (await client.get(f"/v1/venues/{venue_and_device['venue_id']}/trust")).json()
    assert body["scored"] is False
    assert body["score"] is None


@requires_db
@pytest.mark.asyncio
async def test_the_trust_score_is_computed_and_stored(client, venue_and_device):
    from haazir.estimator import trust

    venue_id = venue_and_device["venue_id"]
    async with service_session() as s:
        score = await trust.compute_for_venue(s, venue_id)
        await trust.store(s, venue_id, score)

    body = (await client.get(f"/v1/venues/{venue_id}/trust")).json()
    assert body["scored"] is True
    assert 0 <= body["score"] <= 100
    assert {c["key"] for c in body["components"]} == {
        "Regulatory record", "Review authenticity", "Deal truth",
        "Fact verification", "Kitchen transparency",
    }
    # The components are the score, not a decomposition of it.
    assert sum(c["pts"] for c in body["components"]) == body["score"]


@requires_db
@pytest.mark.asyncio
async def test_a_sealing_costs_most_of_the_regulatory_points_and_recovers(venue_and_device):
    from haazir.estimator import trust

    venue_id = venue_and_device["venue_id"]
    async with service_session() as s:
        await s.execute(
            text(
                """
                INSERT INTO regulatory_event
                       (venue_id, authority, event_type, event_date, source_url, source_name,
                        raw_venue_name, match_confidence, published)
                VALUES (:v, 'Sindh Food Authority', 'sealed', CURRENT_DATE - 2,
                        'https://example.com/a', 'Dawn', 'Live Test Kitchen', 0.97, TRUE)
                """
            ),
            {"v": venue_id},
        )
        recent = await trust.compute_for_venue(s, venue_id)
        # The same venue, judged two years later.
        old = await trust.compute_for_venue(
            s, venue_id, now=dt.date.today() + dt.timedelta(days=730)
        )

    def reg(score):
        return next(c.pts for c in score.components if c.key == "Regulatory record")

    assert reg(recent) <= 6
    assert reg(old) == 40
    assert old.total > recent.total


@requires_db
@pytest.mark.asyncio
async def test_a_clearance_cancels_the_sealing(venue_and_device):
    """§14 rule 6: a later clearance is shown as prominently as the sealing, and it counts."""
    from haazir.estimator import trust

    venue_id = venue_and_device["venue_id"]
    async with service_session() as s:
        await s.execute(
            text(
                """
                INSERT INTO regulatory_event
                       (venue_id, authority, event_type, event_date, source_url, source_name,
                        raw_venue_name, match_confidence, published)
                VALUES (:v, 'Sindh Food Authority', 'sealed', CURRENT_DATE - 10,
                        'https://example.com/a', 'Dawn', 'Live Test Kitchen', 0.97, TRUE)
                """
            ),
            {"v": venue_id},
        )
        sealed = await trust.compute_for_venue(s, venue_id)
        await s.execute(
            text(
                """
                INSERT INTO regulatory_event
                       (venue_id, authority, event_type, event_date, source_url, source_name,
                        raw_venue_name, match_confidence, published)
                VALUES (:v, 'Sindh Food Authority', 'reopened', CURRENT_DATE - 1,
                        'https://example.com/b', 'Dawn', 'Live Test Kitchen', 0.97, TRUE)
                """
            ),
            {"v": venue_id},
        )
        cleared = await trust.compute_for_venue(s, venue_id)

    assert cleared.total > sealed.total
    assert next(c.pts for c in cleared.components if c.key == "Regulatory record") == 40


@requires_db
@pytest.mark.asyncio
async def test_the_venue_card_survives_a_venue_that_has_a_trust_score(client, venue_factory):
    """The card 500'd on production the first time the frontend asked for one.

    `trust_components` was declared `dict | None` while `estimator/trust.py` deliberately
    produces a *list*, ordered the way the venue page renders it. Every existing test passed
    because none of them computed a trust score first, so the field was always None and the
    wrong type was never exercised. The bug needed real data to appear, which is the argument
    for this test: compute the score, then ask for the card.
    """
    from haazir.db import service_session
    from haazir.services import recompute

    venue_id = await venue_factory(name="Trust Card Venue")
    async with service_session() as s:
        await recompute.recompute_trust(s)

    r = await client.get(f"/v1/venues/{venue_id}")
    assert r.status_code == 200, r.text

    card = r.json()
    assert isinstance(card["trust_components"], list)
    assert card["trust_components"], "a scored venue should carry its breakdown"
    first = card["trust_components"][0]
    assert {"key", "pts", "max"} <= set(first)
    # The order is the contract: the venue page renders them top to bottom as given.
    assert first["key"] == "Regulatory record"


@requires_db
@pytest.mark.asyncio
async def test_live_endpoints_accept_a_slug_as_well_as_a_uuid(client, venue_factory):
    """A client that routed by slug should not have to fetch the card to learn the id.

    The card endpoint took either from the start and the live ones took only a UUID, so a page
    that navigated to /v/<slug> could render the venue and then got a 422 asking for its live
    state. The asymmetry was invisible until the frontend was pointed at the API.
    """
    from haazir.db import service_session
    from sqlalchemy import text as sql

    venue_id = await venue_factory(name="Slug Lookup Venue")
    async with service_session() as s:
        slug = await s.scalar(sql("SELECT slug FROM venue WHERE id = :v"), {"v": venue_id})

    by_uuid = await client.get(f"/v1/venues/{venue_id}/live")
    by_slug = await client.get(f"/v1/venues/{slug}/live")

    assert by_uuid.status_code == 200, by_uuid.text
    assert by_slug.status_code == 200, by_slug.text
    assert by_slug.json()["venue_id"] == by_uuid.json()["venue_id"]

    # And a string that is neither is a 404, not a 422 about UUID formatting.
    missing = await client.get("/v1/venues/no-such-venue-anywhere/live")
    assert missing.status_code == 404, missing.text
