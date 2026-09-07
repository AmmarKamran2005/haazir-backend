"""Venue RLS and the protected-column trigger. Plan §4, §12 Phase 7.

Phase 7's criterion is that *an owner cannot modify `tier`, `claimed_by`, `google_rating` or
`trust_score` through any endpoint*. "Any endpoint" is a claim about code that does not exist
yet, so the guarantee is put under the endpoints instead: a BEFORE UPDATE trigger resets those
columns to their old values for anyone who is not an admin or the service path. These tests
write straight to the table, which is the strongest form of the check.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from haazir.db import Claims, service_session, session_scope

from .conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


@pytest.fixture
async def owned_venue(clean_db, venue_factory, user_factory):
    owner_id, _ = await user_factory(role="owner")
    venue_id = await venue_factory("Owned Place", claimed_by=owner_id)
    async with service_session() as s:
        await s.execute(
            text(
                "UPDATE venue SET tier = 'claimed', google_rating = 4.3, "
                "google_review_count = 900, capacity_covers = 80 WHERE id = :v"
            ),
            {"v": venue_id},
        )
    return {"venue_id": venue_id, "owner_id": owner_id}


async def test_an_owner_may_edit_their_own_operational_fields(owned_venue):
    async with session_scope(
        Claims(role="owner", user_id=owned_venue["owner_id"])
    ) as s:
        await s.execute(
            text("UPDATE venue SET capacity_covers = 120, phone = '+922135870000' WHERE id = :v"),
            {"v": owned_venue["venue_id"]},
        )
    async with service_session() as s:
        row = (
            await s.execute(
                text("SELECT capacity_covers, phone FROM venue WHERE id = :v"),
                {"v": owned_venue["venue_id"]},
            )
        ).mappings().one()
    assert row["capacity_covers"] == 120
    assert row["phone"] == "+922135870000"


@pytest.mark.parametrize(
    ("column", "value", "expected"),
    [
        ("tier", "'live'", "claimed"),
        ("google_rating", "5.0", 4.3),
        ("google_review_count", "99999", 900),
    ],
)
async def test_an_owner_cannot_change_a_protected_column(owned_venue, column, value, expected):
    """The update succeeds and silently does nothing to that column. Rejecting it outright
    would be defensible too; resetting means a client sending the whole row back, which is
    what a form does, is not an error."""
    async with session_scope(Claims(role="owner", user_id=owned_venue["owner_id"])) as s:
        await s.execute(
            text(f"UPDATE venue SET {column} = {value} WHERE id = :v"),
            {"v": owned_venue["venue_id"]},
        )
    async with service_session() as s:
        actual = await s.scalar(
            text(f"SELECT {column} FROM venue WHERE id = :v"), {"v": owned_venue["venue_id"]}
        )
    if isinstance(expected, float):
        assert actual == pytest.approx(expected)
    else:
        assert actual == expected


async def test_an_owner_cannot_transfer_a_venue_to_themselves(
    clean_db, venue_factory, user_factory
):
    """`claimed_by` is the whole basis of ownership. If an owner could set it, claiming any
    venue in the city would be one UPDATE."""
    attacker_id, _ = await user_factory(role="owner")
    victim_id, _ = await user_factory(role="owner")
    venue_id = await venue_factory("Someone Else's", claimed_by=victim_id)

    async with session_scope(Claims(role="owner", user_id=attacker_id)) as s:
        await s.execute(
            text("UPDATE venue SET claimed_by = :me WHERE id = :v"),
            {"me": attacker_id, "v": venue_id},
        )
    async with service_session() as s:
        holder = await s.scalar(
            text("SELECT claimed_by FROM venue WHERE id = :v"), {"v": venue_id}
        )
    assert holder == victim_id


async def test_an_owner_cannot_touch_a_venue_they_do_not_own(
    clean_db, venue_factory, user_factory
):
    attacker_id, _ = await user_factory(role="owner")
    victim_id, _ = await user_factory(role="owner")
    venue_id = await venue_factory("Not Yours", claimed_by=victim_id)

    async with session_scope(Claims(role="owner", user_id=attacker_id)) as s:
        result = await s.execute(
            text("UPDATE venue SET capacity_covers = 1 WHERE id = :v"), {"v": venue_id}
        )
    assert result.rowcount == 0  # RLS made the row invisible to the UPDATE


async def test_an_admin_may_change_protected_columns(owned_venue, user_factory):
    admin_id, _ = await user_factory(role="admin")
    async with session_scope(Claims(role="admin", user_id=admin_id)) as s:
        await s.execute(
            text("UPDATE venue SET tier = 'live' WHERE id = :v"),
            {"v": owned_venue["venue_id"]},
        )
    async with service_session() as s:
        tier = await s.scalar(
            text("SELECT tier FROM venue WHERE id = :v"), {"v": owned_venue["venue_id"]}
        )
    assert tier == "live"


async def test_a_diner_cannot_update_any_venue(owned_venue, user_factory):
    diner_id, _ = await user_factory(role="diner")
    async with session_scope(Claims(role="diner", user_id=diner_id)) as s:
        result = await s.execute(
            text("UPDATE venue SET capacity_covers = 5 WHERE id = :v"),
            {"v": owned_venue["venue_id"]},
        )
    assert result.rowcount == 0


async def test_a_hidden_venue_is_invisible_to_the_public(clean_db, venue_factory):
    hidden = await venue_factory("Hidden", status="hidden")
    visible = await venue_factory("Visible")
    async with session_scope() as s:
        ids = [
            r[0]
            for r in (
                await s.execute(text("SELECT id FROM venue WHERE id = ANY(:ids)"),
                                {"ids": [hidden, visible]})
            ).all()
        ]
    assert ids == [visible]


# --- trust and regulatory ----------------------------------------------------


async def test_a_venue_cannot_delete_a_record_about_itself(owned_venue):
    """§14 rule 6: right of reply, not right of removal. There is no INSERT, UPDATE or DELETE
    policy on `regulatory_event` for any user role."""
    async with service_session() as s:
        await s.execute(
            text(
                """
                INSERT INTO regulatory_event
                       (venue_id, authority, event_type, event_date, source_url,
                        source_name, raw_venue_name, match_confidence, published)
                VALUES (:v, 'Sindh Food Authority', 'sealed', CURRENT_DATE,
                        'https://example.test/a', 'Dawn', 'Owned Place', 0.97, TRUE)
                """
            ),
            {"v": owned_venue["venue_id"]},
        )

    async with session_scope(Claims(role="owner", user_id=owned_venue["owner_id"])) as s:
        result = await s.execute(
            text("DELETE FROM regulatory_event WHERE venue_id = :v"),
            {"v": owned_venue["venue_id"]},
        )
        assert result.rowcount == 0
        visible = await s.scalar(
            text("SELECT count(*) FROM regulatory_event WHERE venue_id = :v"),
            {"v": owned_venue["venue_id"]},
        )
    assert visible == 1  # readable, because it is published, and not removable


async def test_an_unpublished_record_is_not_visible_to_anyone(owned_venue):
    """§10.2: below 0.90 match confidence nothing reaches a diner. A wrong match publishes
    "sealed for expired meat" against an innocent business."""
    async with service_session() as s:
        await s.execute(
            text(
                """
                INSERT INTO regulatory_event
                       (venue_id, authority, event_type, event_date, source_url,
                        source_name, raw_venue_name, match_confidence, published)
                VALUES (:v, 'Sindh Food Authority', 'fined', CURRENT_DATE,
                        'https://example.test/b', 'Geo', 'Owned Plaice', 0.62, FALSE)
                """
            ),
            {"v": owned_venue["venue_id"]},
        )
    async with session_scope() as s:
        n = await s.scalar(
            text("SELECT count(*) FROM regulatory_event WHERE venue_id = :v"),
            {"v": owned_venue["venue_id"]},
        )
    assert n == 0


async def test_the_publish_threshold_is_enforced_by_the_database(owned_venue):
    """Not only by the ingestion code that happens to write these rows today."""
    from sqlalchemy.exc import IntegrityError

    async with service_session() as s:
        with pytest.raises(IntegrityError):
            await s.execute(
                text(
                    """
                    INSERT INTO regulatory_event
                           (venue_id, authority, event_type, event_date, source_url,
                            source_name, raw_venue_name, match_confidence, published)
                    VALUES (:v, 'Sindh Food Authority', 'sealed', CURRENT_DATE,
                            'https://example.test/c', 'The News', 'Owned Place', 0.71, TRUE)
                    """
                ),
                {"v": owned_venue["venue_id"]},
            )


async def test_a_trust_score_has_no_user_write_path(owned_venue):
    async with service_session() as s:
        await s.execute(
            text(
                "INSERT INTO trust_score (venue_id, score, components) "
                "VALUES (:v, 71, '{\"hygiene\": 0.6}'::jsonb)"
            ),
            {"v": owned_venue["venue_id"]},
        )
    async with session_scope(Claims(role="owner", user_id=owned_venue["owner_id"])) as s:
        assert (
            await s.execute(
                text("UPDATE trust_score SET score = 100 WHERE venue_id = :v"),
                {"v": owned_venue["venue_id"]},
            )
        ).rowcount == 0
        assert (
            await s.scalar(
                text("SELECT score FROM trust_score WHERE venue_id = :v"),
                {"v": owned_venue["venue_id"]},
            )
            == 71
        )
