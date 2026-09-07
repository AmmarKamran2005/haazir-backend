"""The dignity guarantee. Plan §4, §14 rule 5.

*"No group member's budget is returned to anyone, including the creator."*

That sentence is a marketing claim until this file passes. The test the plan singles out as
the one that must exist before anything ships is `test_group_creator_cannot_read_another
_members_constraint`, and it is deliberately written against the database rather than the API,
because an endpoint that forgets to filter is a bug someone will write next month and RLS is
the thing that has to catch it when they do.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from haazir.db import Claims, service_session, session_scope, solver_session

from .conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


async def _make_group(city_id: int, creator_id: uuid.UUID) -> tuple[uuid.UUID, list[uuid.UUID]]:
    async with service_session() as s:
        gid = await s.scalar(
            text(
                """
                INSERT INTO group_session (creator_id, city_id, party_size, expires_at)
                VALUES (:c, :city, 3, now() + INTERVAL '1 day')
             RETURNING id
                """
            ),
            {"c": creator_id, "city": city_id},
        )
        members = []
        for slot, name in enumerate(["Ammar", "Ayesha", "Bilal"], start=1):
            mid = await s.scalar(
                text(
                    """
                    INSERT INTO group_member (group_id, slot, display_name)
                    VALUES (:g, :slot, :name) RETURNING id
                    """
                ),
                {"g": gid, "slot": slot, "name": name},
            )
            members.append(mid)
    return gid, members


async def _write_constraint(gid: uuid.UUID, slot: int, budget: int, diet: list[str]) -> None:
    """Written the way the API writes it: as that guest, under that guest's claims."""
    async with session_scope(Claims(role="guest", group_id=gid, slot=slot)) as s:
        await s.execute(
            text(
                """
                INSERT INTO group_constraint (group_id, member_slot, budget_pkr,
                                              max_travel_min, diet)
                VALUES (:g, :slot, :budget, 30, CAST(:diet AS text[]))
                """
            ),
            {"g": gid, "slot": slot, "budget": budget, "diet": diet},
        )


@pytest.fixture
async def group(karachi_city_id, user_factory, clean_db):
    creator_id, _ = await user_factory(role="diner")
    gid, members = await _make_group(karachi_city_id, creator_id)
    await _write_constraint(gid, 1, 4000, [])
    await _write_constraint(gid, 2, 900, ["no_beef"])
    await _write_constraint(gid, 3, 2500, ["nut_allergy"])
    return {"id": gid, "creator_id": creator_id, "members": members}


async def test_group_creator_cannot_read_another_members_constraint(group):
    """The test the plan names. The creator is a real, authenticated diner and the group is
    theirs; they still get nothing."""
    async with session_scope(Claims(role="diner", user_id=group["creator_id"])) as s:
        rows = (
            await s.execute(
                text("SELECT * FROM group_constraint WHERE group_id = :g"), {"g": group["id"]}
            )
        ).mappings().all()
    assert rows == []


async def test_a_guest_reads_only_its_own_slot(group):
    async with session_scope(Claims(role="guest", group_id=group["id"], slot=2)) as s:
        rows = (
            await s.execute(
                text("SELECT member_slot, budget_pkr FROM group_constraint WHERE group_id = :g"),
                {"g": group["id"]},
            )
        ).mappings().all()
    assert [dict(r) for r in rows] == [{"member_slot": 2, "budget_pkr": 900}]


async def test_a_guest_cannot_write_into_another_slot(group):
    # `pytest.raises` wraps the whole session, not just the statement. Catching the error
    # inside the `async with` would leave `session_scope` to commit a transaction whose last
    # statement failed, and whether that is a no-op or a second error is a driver detail
    # this test has no business depending on.
    with pytest.raises(DBAPIError):
        async with session_scope(Claims(role="guest", group_id=group["id"], slot=2)) as s:
            await s.execute(
                text(
                    "INSERT INTO group_constraint (group_id, member_slot, budget_pkr) "
                    "VALUES (:g, 3, 99999)"
                ),
                {"g": group["id"]},
            )


async def test_a_guest_of_one_group_sees_nothing_of_another(group, karachi_city_id, user_factory):
    other_creator, _ = await user_factory()
    other_gid, _ = await _make_group(karachi_city_id, other_creator)
    await _write_constraint(other_gid, 1, 7777, [])

    async with session_scope(Claims(role="guest", group_id=group["id"], slot=1)) as s:
        leaked = await s.scalar(
            text("SELECT count(*) FROM group_constraint WHERE group_id = :g"),
            {"g": other_gid},
        )
    assert leaked == 0


async def test_an_admin_cannot_read_constraints(group, user_factory):
    """Admin is the operator of the product. §14 rule 5 says "anyone", and it means it."""
    admin_id, _ = await user_factory(role="admin")
    async with session_scope(Claims(role="admin", user_id=admin_id)) as s:
        rows = (
            await s.execute(
                text("SELECT * FROM group_constraint WHERE group_id = :g"), {"g": group["id"]}
            )
        ).mappings().all()
    assert rows == []


async def test_the_service_path_cannot_read_constraints(group):
    """Ingestion and the background jobs use `app.service`. `group_constraint` is the one
    table with no service policy, so even that route sees nothing."""
    async with service_session() as s:
        rows = (
            await s.execute(
                text("SELECT * FROM group_constraint WHERE group_id = :g"), {"g": group["id"]}
            )
        ).mappings().all()
    assert rows == []


async def test_only_the_solver_session_sees_every_row(group):
    async with solver_session(group["id"]) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT member_slot, budget_pkr FROM group_constraint "
                    "WHERE group_id = :g ORDER BY member_slot"
                ),
                {"g": group["id"]},
            )
        ).mappings().all()
    assert [r["budget_pkr"] for r in rows] == [4000, 900, 2500]


async def test_the_solver_session_is_scoped_to_one_group(group, karachi_city_id, user_factory):
    other_creator, _ = await user_factory()
    other_gid, _ = await _make_group(karachi_city_id, other_creator)
    await _write_constraint(other_gid, 1, 7777, [])

    async with solver_session(group["id"]) as s:
        leaked = await s.scalar(
            text("SELECT count(*) FROM group_constraint WHERE group_id = :g"),
            {"g": other_gid},
        )
    assert leaked == 0


async def test_a_request_transaction_cannot_inherit_a_solver_flag(group):
    """The invariant `solver_session`'s docstring depends on.

    `Claims` cannot express `app.solver` and `apply_claims` writes it empty unless asked,
    so a value cannot survive on a pooled connection into the next caller's request.
    """
    async with solver_session(group["id"]) as s:
        assert await s.scalar(text("SELECT current_setting('app.solver', true)")) == "on"

    async with session_scope(Claims(role="diner", user_id=group["creator_id"])) as s:
        assert await s.scalar(text("SELECT current_setting('app.solver', true)")) == ""
        rows = (
            await s.execute(
                text("SELECT * FROM group_constraint WHERE group_id = :g"), {"g": group["id"]}
            )
        ).mappings().all()
    assert rows == []


async def test_the_connection_role_cannot_bypass_rls():
    r"""The check that a table-level assertion cannot make.

    `BYPASSRLS` is a role attribute and it outranks `FORCE ROW LEVEL SECURITY`: a role that
    has it skips every policy, and `\d` still shows a table that looks secured. Neon's
    default role is a member of `neon_superuser` and carries it, so connecting the API as
    that role silently turns migration 0010 into decoration.

    This is not hypothetical. The first run against Neon failed nineteen tests, all saying
    the same thing, because the API was connecting as the owner. Migration 0012 exists
    because of it, and this test is what would have caught it in one line instead of
    nineteen.
    """
    async with service_session() as s:
        role = (
            await s.execute(
                text(
                    "SELECT current_user AS name, "
                    "(SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user) "
                    "AS bypassrls"
                )
            )
        ).mappings().one()
    assert role["bypassrls"] is False, (
        f"connected as {role['name']}, which has BYPASSRLS. Every policy in this file is "
        f"inert. Point DATABASE_URL at haazir_app, not the Neon owner role."
    )


async def test_rls_is_forced_not_merely_enabled():
    """`ENABLE` alone does not apply to the table owner. `FORCE` is the second half; the
    first half is the test above."""
    async with service_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = ANY(:names)"
                ),
                {
                    "names": [
                        "venue",
                        "observation",
                        "regulatory_event",
                        "trust_score",
                        "group_constraint",
                    ]
                },
            )
        ).mappings().all()
    assert len(rows) == 5
    for r in rows:
        assert r["relrowsecurity"], f"{r['relname']}: RLS not enabled"
        assert r["relforcerowsecurity"], f"{r['relname']}: RLS enabled but not FORCEd"


async def test_a_solution_stores_no_inputs(group):
    """`group_solution.satisfaction` carries slots, names and utilities. Never a budget."""
    async with service_session() as s:
        venue_id = await s.scalar(
            text(
                "INSERT INTO venue (slug, name, city_id, geom, venue_type) "
                "VALUES (:slug, 'Solved', (SELECT id FROM city WHERE name='Karachi'), "
                "ST_SetSRID(ST_MakePoint(67.02, 24.86), 4326)::geography, 'restaurant') "
                "RETURNING id"
            ),
            {"slug": f"solved-{uuid.uuid4().hex[:6]}"},
        )
        await s.execute(
            text(
                """
                INSERT INTO group_solution (group_id, venue_id, objective, min_sat, mean_sat,
                                            satisfaction)
                VALUES (:g, :v, 0.71, 0.71, 0.84, CAST(:sat AS jsonb))
                """
            ),
            {
                "g": group["id"],
                "v": venue_id,
                "sat": '[{"slot":1,"name":"Ammar","u":0.88},'
                '{"slot":2,"name":"Ayesha","u":0.71},'
                '{"slot":3,"name":"Bilal","u":0.93}]',
            },
        )

    async with session_scope(Claims(role="diner", user_id=group["creator_id"])) as s:
        stored = await s.scalar(
            text("SELECT satisfaction::text FROM group_solution WHERE group_id = :g"),
            {"g": group["id"]},
        )
    for forbidden in ("budget", "diet", "max_travel", "900", "4000", "2500"):
        assert forbidden not in stored


async def test_expiry_is_recorded_so_a_group_does_not_live_forever(group):
    async with service_session() as s:
        expires = await s.scalar(
            text("SELECT expires_at FROM group_session WHERE id = :g"), {"g": group["id"]}
        )
    assert expires > dt.datetime.now(dt.UTC)
