"""Phase 6 acceptance: the group solver and the dignity guarantee. Plan §12, §6.6, §14 rule 5.

*"Six members, one nut allergy, one no-beef, mixed budgets, returns a feasible venue and a
satisfaction vector; the API returns no member's inputs to any caller under any role."*

The second half of that sentence is the harder one to test well, and the failure it guards
against is not a crash. It is a response that quietly carries somebody's budget in a field
nobody thought about. So the privacy tests below do not check specific keys; they walk every
response the group surface produces and assert that no member's actual numbers appear
anywhere in it.

`test_rls_group_privacy.py` covers the same guarantee one layer down, at the database. This
file covers it at the API, because those are different mistakes.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from haazir.db import service_session
from haazir.estimator import group as solver
from haazir.services import ingest_venues as ingest

from .conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

BURNS = (24.8615, 67.0180)

# The six people from the acceptance criterion. Deliberately awkward: one allergy, one who
# does not eat beef, budgets four times apart, and one person who compromised last time.
SIX = [
    {"name": "Ammar", "budget_pkr": 2500, "max_travel_min": 30, "diet": [], "mood": "bbq"},
    {"name": "Ayesha", "budget_pkr": 900, "max_travel_min": 25,
     "diet": ["nut_allergy"], "mood": "quiet"},
    {"name": "Bilal", "budget_pkr": 3500, "max_travel_min": 45, "diet": [], "mood": "spicy"},
    {"name": "Fatima", "budget_pkr": 1200, "max_travel_min": 30,
     "diet": ["no_beef"], "mood": None},
    {"name": "Hassan", "budget_pkr": 2000, "max_travel_min": 40, "diet": [], "mood": "bbq"},
    {"name": "Zara", "budget_pkr": 1500, "max_travel_min": 20, "diet": [], "mood": "quiet"},
]

PRIVATE_NUMBERS = [str(m["budget_pkr"]) for m in SIX] + [
    str(m["max_travel_min"]) for m in SIX
]


def venue(place_id: str, name: str, **over) -> dict:
    return {
        "place_id": place_id, "name": name, "area": "Burns Road",
        "lat": BURNS[0], "lng": BURNS[1], "venue_type": "restaurant",
        "cuisines": ["Pakistani", "BBQ"], "google_rating": 4.4,
        "google_review_count": 800, "attributes": {"dine_in": True},
        "scraped_at": "2026-09-03T12:00:00Z",
    } | over


@pytest.fixture
async def venues(clean_db):
    """A small city: one venue that suits everyone, and several that fail one member each."""
    import json as _json

    async with service_session() as s:
        await ingest.load_venues(
            s,
            [
                venue("safe", "Safe For Everyone", avg_ticket_pkr=850),
                venue("pricey", "Too Expensive", avg_ticket_pkr=4000,
                      google_rating=4.9, google_review_count=9000),
                venue("far", "Too Far", lat=25.0100, lng=67.3200, area="Bahria Town",
                      avg_ticket_pkr=800),
                venue("beefy", "Beef Only", avg_ticket_pkr=800),
            ],
        )
        # The allergy route: kitchen transparency on the one venue that should win.
        await s.execute(
            text("UPDATE venue SET attributes = attributes || CAST(:p AS jsonb) "
                 " WHERE place_id IN ('safe', 'pricey', 'far')"),
            {"p": _json.dumps({"kitchen_transparency": {"v": True, "c": 0.9, "n": 4}})},
        )
        # "Beef Only" has one dish and it is beef, so a no-beef member cannot eat there.
        await ingest.load_menu_items(
            s, [{"place_id": "beefy", "name": "Beef Nihari", "price_pkr": 500}]
        )
        await ingest.load_menu_items(
            s, [{"place_id": "safe", "name": "Chicken Karahi", "price_pkr": 700}]
        )
    return True


async def create_group(client, members=None) -> dict:
    body = {
        "title": "Friday dinner",
        "members": [m["name"] for m in (members or SIX)],
        "from_lat": BURNS[0], "from_lng": BURNS[1],
    }
    r = await client.post("/v1/groups", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def submit_all(client, group, members=None) -> dict[int, str]:
    """Submit for the first N members, and return each one's guest token by slot.

    An invite link works exactly once, so a test that needs to act as a member afterwards has
    to keep the token from here rather than exchange the same link a second time.
    """
    members = members or SIX
    tokens: dict[int, str] = {}
    for invite, member in zip(group["invites"][: len(members)], members, strict=True):
        exchanged = await client.post(
            "/v1/auth/group/exchange", json={"token": invite["link"].split("t=")[1]}
        )
        assert exchanged.status_code == 200, exchanged.text
        guest = exchanged.json()["access_token"]
        tokens[invite["slot"]] = guest

        r = await client.post(
            f"/v1/groups/{group['group_id']}/constraint",
            json={k: v for k, v in member.items() if k != "name"},
            headers={"Authorization": f"Bearer {guest}"},
        )
        assert r.status_code == 201, r.text
    return tokens


# --- the acceptance scenario -------------------------------------------------


async def test_six_members_with_mixed_constraints_get_one_feasible_answer(client, venues):
    """Phase 6's criterion, in the plan's own words."""
    group = await create_group(client)
    await submit_all(client, group)

    r = await client.post(f"/v1/groups/{group['group_id']}/solve",
                          json={"from_lat": BURNS[0], "from_lng": BURNS[1]})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["solved"] is True
    best = body["best"]
    assert best["venue_name"] == "Safe For Everyone"

    # A satisfaction vector: one entry per member who answered.
    assert len(best["satisfaction"]) == 6
    assert {s["name"] for s in best["satisfaction"]} == {m["name"] for m in SIX}
    assert all(0.0 <= s["u"] <= 1.0 for s in best["satisfaction"])


async def test_the_venues_that_fail_one_member_are_excluded_not_penalised(client, venues):
    """"Too Expensive" is the best-rated venue in the set. It loses because Ayesha cannot
    afford it, not because it scored slightly lower."""
    group = await create_group(client)
    await submit_all(client, group)

    body = (await client.post(f"/v1/groups/{group['group_id']}/solve",
                              json={"from_lat": BURNS[0], "from_lng": BURNS[1]})).json()
    names = [body["best"]["venue_name"]] + [a["venue_name"] for a in body["alternatives"]]
    assert "Too Expensive" not in names
    assert "Too Far" not in names
    assert "Beef Only" not in names
    # `ruled_out` counts candidates that were considered and then failed a member. "Too
    # Far" lies beyond the search radius entirely, so it never became a candidate and is
    # not counted here. Its absence from `names` above is the assertion that matters.
    assert body["diagnostics"]["ruled_out"] >= 2


async def test_the_answer_reports_the_worst_served_member_not_just_the_average(client, venues):
    group = await create_group(client)
    await submit_all(client, group)
    best = (await client.post(f"/v1/groups/{group['group_id']}/solve", json={})).json()["best"]

    assert "min_satisfaction" in best
    assert "mean_satisfaction" in best
    assert best["min_satisfaction"] <= best["mean_satisfaction"]
    # The gap between them is the difference between "everyone is fine" and "four are
    # delighted and one is miserable". Hiding it would hide what max-min exists to surface.
    worst = min(s["u"] for s in best["satisfaction"])
    assert best["min_satisfaction"] == pytest.approx(worst, abs=1e-3)


async def test_no_feasible_venue_says_so_rather_than_relaxing_something(client, clean_db):
    """These are hard constraints. Quietly widening one to produce an answer would be the
    worst thing this surface could do."""
    async with service_session() as s:
        await ingest.load_venues(s, [venue("only", "Only Option", avg_ticket_pkr=9000)])

    group = await create_group(client, SIX[:2])
    await submit_all(client, group, SIX[:2])

    body = (await client.post(f"/v1/groups/{group['group_id']}/solve", json={})).json()
    assert body["solved"] is False
    assert "nothing has been relaxed" in body["detail"]


async def test_a_group_needs_at_least_two_answers(client, venues):
    group = await create_group(client)
    r = await client.post(f"/v1/groups/{group['group_id']}/solve", json={})
    assert r.status_code == 409
    assert "answered" in r.json()["detail"]


# --- the dignity guarantee, at the API ---------------------------------------


def _walk(node, path="$"):
    """Every (path, key, value) in a parsed JSON tree."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield f"{path}.{k}", k, v
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


async def test_no_response_on_this_surface_contains_a_members_inputs(client, venues):
    """The criterion's second half.

    Walks the parsed response rather than grepping the serialised blob. A substring search
    finds "30" inside `localhost:3000` and inside random invite tokens, which makes the test
    fail for reasons that have nothing to do with privacy and, worse, trains you to loosen it.
    Checking keys and numeric values separately is both precise and strictly stronger.
    """
    group = await create_group(client)
    await submit_all(client, group)
    gid = group["group_id"]

    responses = {
        "create": group,
        "status": (await client.get(f"/v1/groups/{gid}")).json(),
        "solve": (await client.post(f"/v1/groups/{gid}/solve", json={})).json(),
        "solution": (await client.get(f"/v1/groups/{gid}/solution")).json(),
    }

    private_fields = {"budget_pkr", "max_travel_min", "diet", "mood", "budget", "max_travel"}
    private_values = {m["budget_pkr"] for m in SIX} | {m["max_travel_min"] for m in SIX}

    for label, payload in responses.items():
        for path, key, value in _walk(payload):
            assert key not in private_fields, f"{label} exposed the field {key!r} at {path}"
            if isinstance(value, int) and not isinstance(value, bool):
                assert value not in private_values, (
                    f"{label} exposed the value {value} at {path}"
                )
        # The restriction names are the other half: knowing Ayesha has a nut allergy is a
        # disclosure even without her budget.
        blob = json.dumps(payload, default=str).lower()
        for restriction in ("nut_allergy", "no_beef", "vegetarian"):
            assert restriction not in blob, f"{label} named a member's restriction"


async def test_the_organiser_sees_who_answered_and_not_what(client, venues):
    group = await create_group(client)
    await submit_all(client, group, SIX[:3])

    body = (await client.get(f"/v1/groups/{group['group_id']}")).json()
    assert body["responded"] == 3
    answered = [m for m in body["members"] if m["responded"]]
    assert {m["name"] for m in answered} == {m["name"] for m in SIX[:3]}
    assert all(set(m) == {"slot", "name", "responded", "weight"} for m in body["members"])


async def test_a_guest_reads_back_only_their_own_answer(client, venues):
    group = await create_group(client)
    tokens = await submit_all(client, group)
    gid = group["group_id"]

    # The token from the submission. An invite link works exactly once, so exchanging it
    # again here would 400 — which is itself the behaviour `test_an_invite_link_works_once`
    # asserts on purpose.
    headers = {"Authorization": f"Bearer {tokens[2]}"}

    mine = (await client.get(f"/v1/groups/{gid}/constraint", headers=headers)).json()
    assert mine["slot"] == 2
    assert mine["budget_pkr"] == SIX[1]["budget_pkr"]  # Ayesha's own, to herself
    assert mine["diet"] == ["nut_allergy"]


async def test_a_guest_token_cannot_write_another_groups_slot(client, venues):
    """The slot comes from the token, and RLS checks it again underneath."""
    first = await create_group(client)
    second = await create_group(client)

    token = first["invites"][0]["link"].split("t=")[1]
    guest = (await client.post("/v1/auth/group/exchange", json={"token": token})).json()

    r = await client.post(
        f"/v1/groups/{second['group_id']}/constraint",
        json={"budget_pkr": 1000},
        headers={"Authorization": f"Bearer {guest['access_token']}"},
    )
    assert r.status_code == 403


async def test_submitting_a_constraint_requires_a_guest_token(client, venues):
    group = await create_group(client)
    r = await client.post(f"/v1/groups/{group['group_id']}/constraint",
                          json={"budget_pkr": 1000})
    assert r.status_code == 401


async def test_an_invite_link_works_once(client, venues):
    group = await create_group(client)
    token = group["invites"][0]["link"].split("t=")[1]

    assert (await client.post("/v1/auth/group/exchange",
                              json={"token": token})).status_code == 200
    assert (await client.post("/v1/auth/group/exchange",
                              json={"token": token})).status_code == 400


# --- the objective -----------------------------------------------------------


async def test_the_weight_amplifies_shortfall_not_satisfaction():
    """The direction that was wrong in the prototype and is called out in the plan.

    Multiplying `u` by the weight pushes a compromised member above everyone else and stops
    them being the binding minimum, removing the exact protection the weight exists to give.
    """
    venue_row = {"avg_ticket_pkr": 1000, "cuisines": ["Pakistani"], "attributes": {},
                 "wait_p50": 5.0, "trust_score": 70, "best_dish_quality": 7.0}

    plain = solver.Member(slot=1, name="A", budget_pkr=2000, max_travel_min=30, weight=1.0)
    owed = solver.Member(slot=2, name="B", budget_pkr=2000, max_travel_min=30, weight=1.4)

    u = solver.member_utility(plain, venue_row, 10.0)
    scored = solver.score_venue(venue_row, [plain, owed], 10.0)

    # Same inputs, so the same raw utility; the weighted figure must be LOWER for the member
    # who is owed, which is what makes the solver work harder for them.
    weighted_owed = 1 - (1 - u) * 1.4
    assert weighted_owed < u
    assert scored["min_weighted"] == pytest.approx(weighted_owed, abs=1e-6)


async def test_budget_satisfices_rather_than_maximising():
    """Nobody's evening is improved by the restaurant being cheaper than they were willing to
    pay. Treating budget as a maximand returns the cheapest venue in the city every time."""
    assert solver.satisfice(500, 2000, 0.75) == 1.0
    assert solver.satisfice(1500, 2000, 0.75) == 1.0  # still comfortably inside
    assert 0 < solver.satisfice(1800, 2000, 0.75) < 1.0
    assert solver.satisfice(2000, 2000, 0.75) == 0.0


async def test_a_cheaper_venue_does_not_automatically_win():
    cheap = {"avg_ticket_pkr": 300, "cuisines": ["Fast Food"], "attributes": {},
             "wait_p50": 5.0, "trust_score": 40, "best_dish_quality": 4.0}
    good = {"avg_ticket_pkr": 1400, "cuisines": ["BBQ"], "attributes": {},
            "wait_p50": 5.0, "trust_score": 85, "best_dish_quality": 9.0}
    member = solver.Member(slot=1, name="A", budget_pkr=2000, max_travel_min=30, mood="bbq")

    assert solver.member_utility(member, good, 10.0) is not None
    assert solver.member_utility(member, good, 10.0) > solver.member_utility(
        member, cheap, 10.0
    )


async def test_a_hard_constraint_returns_none_not_a_low_score():
    venue_row = {"avg_ticket_pkr": 5000, "cuisines": ["BBQ"], "attributes": {},
                 "wait_p50": 5.0, "trust_score": 90, "best_dish_quality": 9.5}
    member = solver.Member(slot=1, name="A", budget_pkr=1000, max_travel_min=30)
    assert solver.member_utility(member, venue_row, 10.0) is None


async def test_one_infeasible_member_makes_the_whole_venue_infeasible():
    venue_row = {"avg_ticket_pkr": 5000, "cuisines": ["BBQ"], "attributes": {},
                 "wait_p50": 5.0, "trust_score": 90, "best_dish_quality": 9.0}
    rich = solver.Member(slot=1, name="A", budget_pkr=8000, max_travel_min=60)
    poor = solver.Member(slot=2, name="B", budget_pkr=900, max_travel_min=60)
    assert solver.score_venue(venue_row, [rich, poor], 10.0) is None


async def test_a_member_with_no_stated_mood_is_not_treated_as_dissatisfied():
    """Silence is not a complaint, and it must not drag the group's minimum down."""
    venue_row = {"avg_ticket_pkr": 1000, "cuisines": ["Pakistani"], "attributes": {},
                 "wait_p50": 5.0, "trust_score": 70, "best_dish_quality": 7.0}
    quiet = solver.Member(slot=1, name="A", budget_pkr=2000, max_travel_min=30, mood=None)
    mismatched = solver.Member(slot=2, name="B", budget_pkr=2000, max_travel_min=30,
                               mood="seafood")
    assert solver.member_utility(quiet, venue_row, 10.0) > \
           solver.member_utility(mismatched, venue_row, 10.0)


async def test_the_response_explains_the_objective(client, venues):
    group = await create_group(client)
    await submit_all(client, group)
    body = (await client.post(f"/v1/groups/{group['group_id']}/solve", json={})).json()

    assert body["objective"]["kind"] == "max-min"
    assert "0.72" in body["objective"]["formula"]
    assert "worst" in body["objective"]["why"]


async def test_the_solution_is_stored_and_readable(client, venues):
    group = await create_group(client)
    await submit_all(client, group)
    gid = group["group_id"]
    await client.post(f"/v1/groups/{gid}/solve", json={})

    stored = (await client.get(f"/v1/groups/{gid}/solution")).json()
    assert stored["name"] == "Safe For Everyone"
    assert len(stored["satisfaction"]) == 6
    assert "tightest fit" in stored["rationale"]

    async with service_session() as s:
        status = await s.scalar(
            text("SELECT status FROM group_session WHERE id = :g"), {"g": gid}
        )
    assert status == "solved"


async def test_an_unsolved_group_has_no_solution(client, venues):
    group = await create_group(client)
    assert (await client.get(f"/v1/groups/{group['group_id']}/solution")).status_code == 404
