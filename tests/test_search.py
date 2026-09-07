"""Phase 4 acceptance: search, and hard constraints that are provably filters. Plan §12, §6.4.

*"Hard constraints are provably filters — a nut-allergy query never returns a venue lacking
the flag."*

"Provably" is the word that shapes this file. It is not enough to check that a constrained
search returns a shorter list: a penalty large enough to sink most venues would pass that and
still, occasionally, let one through. So each test below asserts the stronger property, that
**every** returned venue satisfies the constraint, against a fixture built so that a penalty
would visibly fail — the violating venues are made the most attractive ones in the set.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from haazir.db import service_session
from haazir.estimator import scoring
from haazir.estimator.scoring import Query, search
from haazir.services import ingest_venues as ingest

from .conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

BURNS = (24.8615, 67.0180)


def venue(place_id: str, name: str, **over) -> dict:
    return {
        "place_id": place_id,
        "name": name,
        "area": "Burns Road",
        "lat": BURNS[0],
        "lng": BURNS[1],
        "venue_type": "restaurant",
        "cuisines": ["Pakistani", "BBQ"],
        "google_rating": 4.5,
        "google_review_count": 1200,
        "attributes": {"dine_in": True},
        "scraped_at": "2026-09-03T12:00:00Z",
    } | over


async def set_attr(place_id: str, key: str, value, confidence: float = 0.9) -> None:
    import json

    async with service_session() as s:
        await s.execute(
            text(
                "UPDATE venue SET attributes = attributes || CAST(:patch AS jsonb) "
                "WHERE place_id = :p"
            ),
            {"p": place_id,
             "patch": json.dumps({key: {"v": value, "c": confidence, "n": 5}})},
        )


async def set_trust(place_id: str, score: int) -> None:
    async with service_session() as s:
        await s.execute(
            text(
                "INSERT INTO trust_score (venue_id, score, components) "
                "SELECT id, :s, '{}'::jsonb FROM venue WHERE place_id = :p "
                "ON CONFLICT (venue_id) DO UPDATE SET score = EXCLUDED.score"
            ),
            {"p": place_id, "s": score},
        )


@pytest.fixture
async def two_venues(clean_db):
    """One venue that satisfies nothing and is otherwise the best in the city, one that does.

    The violating venue is deliberately the more attractive: better rated, more reviewed,
    identical location. If constraints were scored rather than filtered, it would win.
    """
    async with service_session() as s:
        await ingest.load_venues(
            s,
            [
                venue("tempting", "Tempting But Unsuitable",
                      google_rating=4.9, google_review_count=9000),
                venue("suitable", "Plain But Suitable",
                      google_rating=3.9, google_review_count=60),
            ],
        )
    return {"bad": "tempting", "good": "suitable"}


async def run(q: Query) -> list[str]:
    async with service_session() as s:
        result = await search(s, q)
    return [r.name for r in result["results"]]


def base_query(**over) -> Query:
    defaults = {"from_lat": BURNS[0], "from_lng": BURNS[1], "max_travel": 60, "limit": 50}
    return Query(**(defaults | over))


# --- both venues are reachable without constraints ---------------------------


async def test_without_constraints_both_venues_are_returned(two_venues):
    """The control. Everything below depends on the violating venue being findable."""
    names = await run(base_query())
    assert "Tempting But Unsuitable" in names
    assert "Plain But Suitable" in names


# --- access facts ------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "attribute"),
    [
        ("needs_prayer", "prayer_area"),
        ("needs_family", "family_section"),
        ("needs_ramp", "wheelchair_accessible"),
        ("needs_card", "accepts_cards"),
    ],
)
async def test_an_access_requirement_excludes_every_venue_lacking_it(
    two_venues, flag, attribute
):
    await set_attr("suitable", attribute, True)

    names = await run(base_query(**{flag: True}))
    assert names == ["Plain But Suitable"], (
        f"{flag} let a venue through that has no {attribute}"
    )


async def test_an_unconfirmed_fact_does_not_satisfy_a_stated_need(two_venues):
    """`{"v": true, "c": 0.2}` is somebody's guess. Treating it as a yes is how an app sends
    a wheelchair user to a restaurant they cannot enter."""
    await set_attr("tempting", "wheelchair_accessible", True, confidence=0.2)
    await set_attr("suitable", "wheelchair_accessible", True, confidence=0.9)

    names = await run(base_query(needs_ramp=True))
    assert names == ["Plain But Suitable"]


async def test_a_fact_recorded_as_false_never_satisfies_it(two_venues):
    await set_attr("tempting", "prayer_area", False, confidence=0.95)
    await set_attr("suitable", "prayer_area", True)

    names = await run(base_query(needs_prayer=True))
    assert names == ["Plain But Suitable"]


async def test_several_access_requirements_all_apply(two_venues):
    await set_attr("suitable", "prayer_area", True)
    await set_attr("suitable", "family_section", True)
    await set_attr("tempting", "prayer_area", True)  # satisfies only one

    names = await run(base_query(needs_prayer=True, needs_family=True))
    assert names == ["Plain But Suitable"]


# --- the nut-allergy criterion -----------------------------------------------


async def test_a_nut_allergy_query_never_returns_a_venue_without_the_flag(two_venues):
    """Phase 4's acceptance criterion, in the plan's own words.

    The safe venue is the *less* appealing of the two by every scored factor, so a result
    containing only it can only be the work of a filter.
    """
    await set_attr("suitable", "kitchen_transparency", True)

    names = await run(base_query(diet=["nut_allergy"]))
    assert names == ["Plain But Suitable"]


async def test_a_high_trust_score_also_satisfies_the_allergy_constraint(two_venues):
    """Kitchen transparency is one route; a strong verified record is the other. Both are
    checkable claims, which is the point."""
    await set_trust("tempting", 85)
    await set_trust("suitable", 40)

    names = await run(base_query(diet=["nut_allergy"]))
    assert names == ["Tempting But Unsuitable"]


async def test_neither_route_means_no_results_rather_than_an_unsafe_one(two_venues):
    """An empty list is the correct answer here. The relaxation path deliberately does not
    loosen a dietary constraint, and this is why."""
    names = await run(base_query(diet=["nut_allergy"]))
    assert names == []


# --- budget and travel -------------------------------------------------------


async def test_a_typed_budget_is_a_ceiling_not_a_preference(clean_db):
    """"Under Rs 2,500" returning a Rs 5,800 restaurant leaves the counterfactual panel with
    nothing to offer, which is what the slack allowance is calibrated against."""
    async with service_session() as s:
        await ingest.load_venues(
            s,
            [
                venue("cheap", "Within Budget", avg_ticket_pkr=800),
                venue("dear", "Far Over Budget", avg_ticket_pkr=5800,
                      google_rating=5.0, google_review_count=20000),
            ],
        )
    names = await run(base_query(budget=1000))
    assert names == ["Within Budget"]


async def test_the_budget_ceiling_allows_a_little_slack_for_drinks(clean_db):
    async with service_session() as s:
        await ingest.load_venues(
            s, [venue("edge", "Just Over", avg_ticket_pkr=1080)]
        )
    assert await run(base_query(budget=1000)) == ["Just Over"]  # 1000 * 1.12 = 1120
    assert await run(base_query(budget=900)) == []              # 900 * 1.12 = 1008


async def test_a_venue_with_no_known_price_is_not_silently_dropped(clean_db):
    """Excluding unpriced venues would quietly shrink the city to the ones that happen to
    have been priced, which is most of the dataset gone."""
    async with service_session() as s:
        await ingest.load_venues(s, [venue("unknown", "Price Unknown")])
    assert await run(base_query(budget=500)) == ["Price Unknown"]


async def test_travel_is_a_filter_too(clean_db):
    async with service_session() as s:
        await ingest.load_venues(
            s,
            [
                venue("near", "Around The Corner"),
                # Bahria Town, roughly 40 km out.
                venue("far", "Very Far Away", lat=25.0100, lng=67.3200,
                      area="Bahria Town", google_rating=5.0, google_review_count=50000),
            ],
        )
    names = await run(base_query(max_travel=15))
    assert names == ["Around The Corner"]


# --- the score is a read-out -------------------------------------------------


async def test_every_result_carries_the_terms_of_its_own_score(two_venues):
    async with service_session() as s:
        result = await search(s, base_query())
    first = result["results"][0].as_dict()

    assert set(first["factors"]) == set(scoring.WEIGHTS)
    assert first["weights"] == scoring.WEIGHTS
    # The total is the weighted sum of the terms shown, not a number arrived at separately.
    recomputed = sum(scoring.WEIGHTS[k] * v for k, v in first["factors"].items())
    assert first["score"] == pytest.approx(recomputed, abs=1e-3)


async def test_the_weights_sum_to_one():
    assert sum(scoring.WEIGHTS.values()) == pytest.approx(1.0)


async def test_results_are_ordered_by_score(two_venues):
    async with service_session() as s:
        result = await search(s, base_query())
    scores = [r.total for r in result["results"]]
    assert scores == sorted(scores, reverse=True)


async def test_every_result_says_whether_its_occupancy_was_measured(two_venues):
    """§14 rule 1. Nothing has reported on these venues, so every row must say `archetype`
    or `prior`, never `live`."""
    async with service_session() as s:
        result = await search(s, base_query())
    assert result["results"]
    assert all(r.occupancy_source in {"prior", "archetype"} for r in result["results"])


# --- never an empty list -----------------------------------------------------


async def test_an_over_tight_travel_limit_relaxes_rather_than_returning_nothing(clean_db):
    from haazir.estimator.scoring import search_with_relaxation

    async with service_session() as s:
        await ingest.load_venues(
            s, [venue("far", "Out Of Town", lat=25.0100, lng=67.3200, area="Bahria Town")]
        )
        result = await search_with_relaxation(s, base_query(max_travel=10))

    assert result["results"], "the UI never renders an empty list; the API must relax instead"
    assert result["relaxed"] is True
    assert "minutes" in result["relaxed_note"]


async def test_relaxation_never_loosens_a_dietary_constraint(two_venues):
    """Travel is a diner's own trade-off to make. An allergy is not."""
    from haazir.estimator.scoring import search_with_relaxation

    async with service_session() as s:
        result = await search_with_relaxation(s, base_query(diet=["nut_allergy"]))
    assert result["results"] == []


# --- the endpoint ------------------------------------------------------------


async def test_search_is_public(client, two_venues):
    r = await client.post("/v1/search", json={"from_lat": BURNS[0], "from_lng": BURNS[1]})
    assert r.status_code == 200
    assert r.json()["count"] > 0


async def test_the_endpoint_returns_the_weights_and_the_live_fraction(client, two_venues):
    body = (
        await client.post(
            "/v1/search", json={"from_lat": BURNS[0], "from_lng": BURNS[1], "limit": 5}
        )
    ).json()
    assert body["weights"] == scoring.WEIGHTS
    assert body["live_fraction"] == 0.0  # nothing has reported yet, and it says so
    assert body["candidates_considered"] >= body["count"]


async def test_the_endpoint_enforces_the_allergy_filter(client, two_venues):
    await set_attr("suitable", "kitchen_transparency", True)
    body = (
        await client.post(
            "/v1/search",
            json={"from_lat": BURNS[0], "from_lng": BURNS[1], "diet": ["nut_allergy"]},
        )
    ).json()
    assert [r["name"] for r in body["results"]] == ["Plain But Suitable"]


@requires_db
@pytest.mark.asyncio
async def test_a_named_area_and_cuisine_in_free_text_actually_filter(client, venue_factory):
    """The filters existed and nothing ever set them.

    `v.area_id` and the cuisines predicate were both in `hard_filter_sql` from the start, but
    no code path populated `Query.area_id` or `Query.cuisine` from what a person typed. So
    "chinese" returned a bakery and "biryani in North Nazimabad" returned Saddar — the text
    went into the request and was never read.

    The cuisine half also needed the comparison to stop being case-sensitive: the column holds
    "Biryani" and people type "biryani", so `@>` matched nothing the moment it was fed.
    """
    from haazir.services import locate

    assert locate.cuisine_for("i want chinese tonight") == "chinese"
    assert locate.cuisine_for("kuch bhi") is None
    # Longest alias wins, so a two-word cuisine is not lost to a one-word one.
    assert locate.cuisine_for("fast food chahiye") == "fast food"

    r = await client.post(
        "/v1/search", json={"text": "chinese", "from_lat": 24.8615, "from_lng": 67.0180}
    )
    assert r.status_code == 200, r.text
    for row in r.json()["results"]:
        assert any(c.lower() == "chinese" for c in row["cuisines"]), row["name"]


@requires_db
@pytest.mark.asyncio
async def test_the_longer_area_name_wins(client):
    """North Nazimabad and Nazimabad are both areas, and one contains the other.

    A naive scan finds the shorter name inside the longer one and sends the diner to a
    different part of the city — which is exactly the failure this whole resolver exists to
    fix, reintroduced by the fix itself.
    """
    from haazir.db import service_session
    from haazir.services import locate

    async with service_session() as s:
        north = await locate.area_id_for(s, "biryani in north nazimabad")
        plain = await locate.area_id_for(s, "biryani in nazimabad")
        from sqlalchemy import text as sql

        names = {
            r.id: r.name
            for r in (await s.execute(sql("SELECT id, name FROM area"))).all()
        }

    assert names.get(north) == "North Nazimabad"
    assert names.get(plain) == "Nazimabad"
    assert north != plain


@requires_db
@pytest.mark.asyncio
async def test_a_misspelt_area_and_cuisine_still_resolve():
    """People type "nazimbad" and "chineese", and an exact scan throws the word away silently.

    Word-level rather than whole-phrase: "biryani in north nazimbad" scores badly against
    "North Nazimabad" as one string and almost perfectly word by word. Every word of the name
    must find a partner, which is what stops "Nazimabad" quietly satisfying "North Nazimabad".
    """
    from haazir.db import service_session
    from haazir.services import locate
    from sqlalchemy import text as sql

    async with service_session() as s:
        names = {r.id: r.name for r in (await s.execute(sql("SELECT id, name FROM area"))).all()}
        assert names.get(await locate.area_id_for(s, "biryani in north nazimbad")) == "North Nazimabad"
        assert names.get(await locate.area_id_for(s, "chineese in clifon")) == "Clifton"
        assert names.get(await locate.area_id_for(s, "sadar mein biryani")) == "Saddar"
        # And the distinction survives fuzzing: these are two different places.
        assert names.get(await locate.area_id_for(s, "biryani in nazimabad")) == "Nazimabad"
        # A sentence naming no area must not be forced into one.
        assert await locate.area_id_for(s, "kuch bhi acha") is None

    assert locate.cuisine_for("chineese") == "chinese"
    assert locate.cuisine_for("biryni chahiye") == "biryani"
    assert locate.cuisine_for("sasta aur jaldi") is None


@requires_db
@pytest.mark.asyncio
async def test_the_model_cannot_invent_an_area(monkeypatch):
    """The closed lists go into the prompt and the answer is checked against them coming back.

    A model that returns "Gulshan-e-Maymar" — a real Karachi neighbourhood that is not in this
    catalogue — must resolve to nothing rather than to a filter that matches no venue and looks
    like an empty city.
    """
    from haazir.db import service_session
    from haazir.services import llm, locate

    async def fake_call(model, system, prompt, max_tokens=160):
        return '{"area": "Gulshan-e-Maymar", "cuisine": "Klingon", "dish": "nihari"}'

    monkeypatch.setattr(llm, "_call", fake_call)
    monkeypatch.setattr(llm, "available", lambda: True)

    async with service_session() as s:
        out = await locate.resolve(s, "something with no keyword the rules can read")

    assert out["area_id"] is None, "an area outside the catalogue must not become a filter"
    assert out["cuisine"] is None, "a cuisine outside the closed list must be dropped"
    assert out["dish"] == "nihari", "a free-text dish is allowed through"
