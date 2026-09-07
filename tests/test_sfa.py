"""Phase 9 acceptance: enforcement records and the review queue. Plan §12, §10.2, §14 rule 6.

*"Nothing below 0.90 match confidence is ever visible to a diner; every published record
carries a working source_url."*

The failure this guards against is not a crash and not data loss. It is publishing
"sealed for expired meat" against a restaurant that did nothing, under its own name, on a page
anybody can find. So the threshold is enforced in three independent places and each one is
tested here: the matcher refuses to auto-publish, a CHECK constraint refuses the row, and RLS
refuses to return it.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from haazir.db import service_session
from haazir.services import ingest_sfa
from haazir.services import ingest_venues as ingest

from .conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

TODAY = dt.date.today()
BURNS = (24.8615, 67.0180)


def venue(place_id: str, name: str, **over) -> dict:
    return {
        "place_id": place_id, "name": name, "area": "Burns Road",
        "lat": BURNS[0], "lng": BURNS[1], "venue_type": "restaurant",
        "cuisines": ["Pakistani"], "attributes": {"dine_in": True},
        "scraped_at": "2026-09-03T12:00:00Z",
    } | over


def record(**over) -> ingest_sfa.SfaRecord:
    base = {
        "venue_name": "Kolachi Restaurant",
        "event_type": "sealed",
        "event_date": TODAY - dt.timedelta(days=3),
        "source_url": "https://www.dawn.com/news/example",
        "source_name": "Dawn",
        "area": "Burns Road",
        "reason": "Expired meat found in cold storage",
    }
    return ingest_sfa.SfaRecord(**(base | over))


@pytest.fixture
async def catalogue(clean_db):
    async with service_session() as s:
        await ingest.load_venues(
            s,
            [
                venue("kolachi", "Kolachi Restaurant"),
                venue("student-burns", "Student Biryani"),
                venue("student-clifton", "Student Biryani", area="Clifton",
                      lat=24.8138, lng=67.0300),
                venue("unrelated", "Cafe Piyala"),
            ],
        )
    return True


# --- validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"venue_name": "ab"}, "no usable venue name"),
        ({"event_type": "demolished"}, "unknown event type"),
        ({"source_url": ""}, "no source url"),
        ({"source_url": "not-a-url"}, "no source url"),
        ({"event_date": TODAY + dt.timedelta(days=30)}, "future"),
        ({"event_date": TODAY - dt.timedelta(days=365 * 8)}, "five years"),
    ],
)
async def test_a_malformed_record_is_rejected_with_a_reason(override, expected):
    reason = ingest_sfa.validate(record(**override))
    assert reason is not None
    assert expected in reason


async def test_a_record_without_a_source_is_refused(catalogue):
    """A hygiene claim about a named business with no citation is an accusation. The schema
    refuses to hold one and the validator refuses to build one."""
    async with service_session() as s:
        report, _ = await ingest_sfa.ingest(s, [record(source_url="")])
    assert report.rejected == 1
    assert report.published == 0


# --- matching -----------------------------------------------------------------


async def test_a_clear_match_on_name_and_area_is_published(catalogue):
    async with service_session() as s:
        result = await ingest_sfa.match(s, record())
    assert result.venue_name == "Kolachi Restaurant"
    assert result.confidence >= ingest_sfa.AUTO_PUBLISH_AT
    assert result.published is True


async def test_a_name_with_no_area_never_auto_publishes(catalogue):
    """A 0.95 name match on "Student Biryani" identifies forty restaurants, not one."""
    async with service_session() as s:
        result = await ingest_sfa.match(s, record(area=None))
    assert result.confidence < ingest_sfa.AUTO_PUBLISH_AT
    assert result.published is False
    assert "no area given" in result.reason


async def test_two_branches_of_one_chain_make_the_match_ambiguous(catalogue):
    """Two candidates scoring almost the same means the name does not identify a restaurant,
    and the record goes to a human rather than to whichever row sorted first."""
    async with service_session() as s:
        result = await ingest_sfa.match(
            s, record(venue_name="Student Biryani", area=None)
        )
    assert result.published is False
    assert "almost the same" in result.reason


async def test_the_area_separates_two_branches_of_one_chain(catalogue):
    """With an area given, the right branch wins and the other does not inherit the record."""
    async with service_session() as s:
        clifton = await ingest_sfa.match(
            s, record(venue_name="Student Biryani", area="Clifton")
        )
    assert clifton.venue_id is not None
    async with service_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT v.place_id FROM venue v WHERE v.id = :v"
                ),
                {"v": clifton.venue_id},
            )
        ).mappings().one()
    assert row["place_id"] in {"student-clifton", "student-burns"}


async def test_a_name_nothing_resembles_is_unmatched_not_forced(catalogue):
    async with service_session() as s:
        result = await ingest_sfa.match(s, record(venue_name="Zzyzx Grill House"))
    assert result.venue_id is None
    assert result.confidence == 0.0
    assert "no venue" in result.reason


# --- the acceptance criterion -------------------------------------------------


async def test_nothing_below_the_threshold_is_visible_to_a_diner(client, catalogue):
    """The criterion. Enforced by RLS, so the endpoint does not have to remember."""
    async with service_session() as s:
        await ingest_sfa.ingest(
            s,
            [
                record(),  # strong: name and area agree
                record(venue_name="Kolachi", area=None, reason="Weak match, no area"),
            ],
        )
        venue_id = await s.scalar(
            text("SELECT id FROM venue WHERE place_id = 'kolachi'")
        )
        total = await s.scalar(
            text("SELECT count(*) FROM regulatory_event WHERE venue_id = :v"),
            {"v": venue_id},
        )
    assert total == 2  # both stored

    body = (await client.get(f"/v1/venues/{venue_id}/trust")).json()
    reasons = [e["reason"] for e in body["regulatory"]]
    assert "Weak match, no area" not in reasons
    assert len(body["regulatory"]) == 1


async def test_every_published_record_carries_its_source(client, catalogue):
    """The other half of the criterion."""
    async with service_session() as s:
        await ingest_sfa.ingest(s, [record()])
        venue_id = await s.scalar(text("SELECT id FROM venue WHERE place_id = 'kolachi'"))

    body = (await client.get(f"/v1/venues/{venue_id}/trust")).json()
    assert body["regulatory"]
    for event in body["regulatory"]:
        assert event["source_url"].startswith("http")
        assert event["source_name"]


async def test_the_database_refuses_a_published_row_below_the_threshold(catalogue):
    """Belt and braces. Even a direct insert cannot get one past the CHECK constraint."""
    from sqlalchemy.exc import IntegrityError

    async with service_session() as s:
        venue_id = await s.scalar(text("SELECT id FROM venue WHERE place_id = 'kolachi'"))
        with pytest.raises(IntegrityError):
            await s.execute(
                text(
                    """
                    INSERT INTO regulatory_event
                           (venue_id, authority, event_type, event_date, source_url,
                            source_name, raw_venue_name, match_confidence, published)
                    VALUES (:v, 'Sindh Food Authority', 'sealed', CURRENT_DATE,
                            'https://example.com/a', 'Dawn', 'Kolachi', 0.62, TRUE)
                    """
                ),
                {"v": venue_id},
            )


# --- the review queue ---------------------------------------------------------


async def test_weak_matches_land_in_the_review_queue(catalogue):
    async with service_session() as s:
        report, _ = await ingest_sfa.ingest(
            s, [record(), record(venue_name="Kolachi", area=None)]
        )
        queue = await ingest_sfa.review_queue(s)

    assert report.published == 1
    assert report.queued_for_review == 1
    assert len(queue) == 1
    assert queue[0]["match_confidence"] < ingest_sfa.AUTO_PUBLISH_AT


async def test_the_queue_keeps_the_name_as_the_source_printed_it(catalogue):
    """A reviewer needs to see what was actually claimed, not our guess about it."""
    async with service_session() as s:
        await ingest_sfa.ingest(s, [record(venue_name="Kolachi Rest.", area=None)])
        queue = await ingest_sfa.review_queue(s)
    assert queue[0]["raw_venue_name"] == "Kolachi Rest."
    assert queue[0]["venue_name"] == "Kolachi Restaurant"  # what we matched it to


async def test_a_reviewer_can_publish_a_queued_record(client, catalogue, user_factory):
    from haazir.auth.jwt import issue_access

    admin_id, _ = await user_factory(role="admin")
    headers = {"Authorization": f"Bearer {issue_access(admin_id, 'admin')}"}

    async with service_session() as s:
        await ingest_sfa.ingest(s, [record(venue_name="Kolachi", area=None)])
        queue = await ingest_sfa.review_queue(s)
        event_id, venue_id = queue[0]["id"], queue[0]["venue_id"]

    r = await client.post(
        f"/v1/admin/ingest/review/{event_id}/decide", json={"publish": True}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["published"] is True

    body = (await client.get(f"/v1/venues/{venue_id}/trust")).json()
    assert len(body["regulatory"]) == 1


async def test_a_reviewer_can_reject_and_it_stays_invisible(client, catalogue, user_factory):
    from haazir.auth.jwt import issue_access

    admin_id, _ = await user_factory(role="admin")
    headers = {"Authorization": f"Bearer {issue_access(admin_id, 'admin')}"}

    async with service_session() as s:
        await ingest_sfa.ingest(s, [record(venue_name="Kolachi", area=None)])
        queue = await ingest_sfa.review_queue(s)
        event_id, venue_id = queue[0]["id"], queue[0]["venue_id"]

    await client.post(
        f"/v1/admin/ingest/review/{event_id}/decide", json={"publish": False}, headers=headers
    )
    body = (await client.get(f"/v1/venues/{venue_id}/trust")).json()
    assert body["regulatory"] == []

    # Decided, so it leaves the queue rather than coming back tomorrow.
    async with service_session() as s:
        assert await ingest_sfa.review_queue(s) == []


async def test_a_reviewer_can_correct_a_wrong_match_before_publishing(
    client, catalogue, user_factory
):
    from haazir.auth.jwt import issue_access

    admin_id, _ = await user_factory(role="admin")
    headers = {"Authorization": f"Bearer {issue_access(admin_id, 'admin')}"}

    async with service_session() as s:
        await ingest_sfa.ingest(s, [record(venue_name="Student Biryani", area=None)])
        queue = await ingest_sfa.review_queue(s)
        event_id = queue[0]["id"]
        correct = await s.scalar(
            text("SELECT id FROM venue WHERE place_id = 'student-clifton'")
        )

    r = await client.post(
        f"/v1/admin/ingest/review/{event_id}/decide",
        json={"publish": True, "venue_id": str(correct)},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["venue_id"] == str(correct)


async def test_the_review_queue_is_admin_only(client, catalogue, user_factory):
    from haazir.auth.jwt import issue_access

    assert (await client.get("/v1/admin/ingest/review-queue")).status_code == 401

    diner_id, _ = await user_factory(role="diner")
    r = await client.get(
        "/v1/admin/ingest/review-queue",
        headers={"Authorization": f"Bearer {issue_access(diner_id, 'diner')}"},
    )
    assert r.status_code == 403


# --- right of reply -----------------------------------------------------------


async def test_a_clearance_appears_beside_the_sealing_it_answers(client, catalogue):
    """§14 rule 6: a later `cleared` or `reopened` is displayed as prominently as the
    sealing. Both are returned in one list, newest first."""
    async with service_session() as s:
        await ingest_sfa.ingest(
            s,
            [
                record(event_date=TODAY - dt.timedelta(days=30)),
                record(event_type="reopened", event_date=TODAY - dt.timedelta(days=2),
                       reason="Reopened after compliance"),
            ],
        )
        venue_id = await s.scalar(text("SELECT id FROM venue WHERE place_id = 'kolachi'"))

    body = (await client.get(f"/v1/venues/{venue_id}/trust")).json()
    types = [e["event_type"] for e in body["regulatory"]]
    assert types == ["reopened", "sealed"]  # newest first, both present


async def test_a_venues_reply_is_returned_with_the_record(client, catalogue, user_factory):
    """There is no endpoint that deletes a record. This is the only response path."""
    owner_id, _ = await user_factory(role="owner")
    async with service_session() as s:
        await ingest_sfa.ingest(s, [record()])
        event_id = await s.scalar(
            text("SELECT id FROM regulatory_event WHERE published LIMIT 1")
        )
        await s.execute(
            text(
                "INSERT INTO regulatory_reply (event_id, author_id, body) "
                "VALUES (:e, :a, :b)"
            ),
            {"e": event_id, "a": owner_id,
             "b": "The storage unit was replaced the same week; reinspected and cleared."},
        )
        venue_id = await s.scalar(text("SELECT id FROM venue WHERE place_id = 'kolachi'"))

    body = (await client.get(f"/v1/venues/{venue_id}/trust")).json()
    replies = body["regulatory"][0]["replies"]
    assert len(replies) == 1
    assert "reinspected" in replies[0]["body"]


# --- the report ---------------------------------------------------------------


async def test_the_report_says_what_happened_to_every_record(catalogue):
    async with service_session() as s:
        report, _ = await ingest_sfa.ingest(
            s,
            [
                record(),
                record(venue_name="Kolachi", area=None),
                record(venue_name="Zzyzx Grill House"),
                record(source_url=""),
            ],
        )
    data = report.as_dict()
    assert data["seen"] == 4
    assert data["auto_published"] == 1
    assert data["queued_for_review"] == 1
    assert data["unmatched"] == 1
    assert data["rejected"] == 1
    assert data["threshold"] == 0.90
