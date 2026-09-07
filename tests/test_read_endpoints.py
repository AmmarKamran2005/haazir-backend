"""The discovery read APIs. Plan §7, §12 Phase 3.

The recurring assertion in this file is that a number never travels without saying where it
came from. Almost every occupancy figure in the product today is a prior rather than an
observation, and a response that renders the two identically is the failure §14 rule 1
exists to forbid.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from haazir.db import service_session
from haazir.services import ingest_venues as ingest

from .conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def venue(place_id: str, name: str, **over) -> dict:
    return {
        "place_id": place_id,
        "name": name,
        "area": "Burns Road",
        "lat": 24.8615,
        "lng": 67.0180,
        "venue_type": "restaurant",
        "cuisines": ["Pakistani", "BBQ"],
        "phone": "+922135870000",
        "google_rating": 4.4,
        "google_review_count": 900,
        "attributes": {"dine_in": True},
        "scraped_at": "2026-09-03T12:00:00Z",
    } | over


@pytest.fixture
async def seeded(clean_db):
    """Six venues on Burns Road, five of them pricing bihari boti at known prices."""
    venues = [venue(f"p{i}", f"Venue {i}") for i in range(6)]
    venues.append(
        venue("p-quiet", "Quiet Cafe", venue_type="cafe", cuisines=["Coffee"],
              area="Clifton", lat=24.8138, lng=67.0300)
    )
    menus = [
        {"place_id": f"p{i}", "name": nm, "price_pkr": price, "source": "foodpanda",
         "price_seen_at": "2026-08-20"}
        for i, (nm, price) in enumerate(
            [("Bihari Boti", 700), ("Behari boti", 800), ("Beef Bihari Boti", 900),
             ("Chicken Behari Boti", 600), ("Bihari Kabab", 1000)]
        )
    ]
    async with service_session() as s:
        await ingest.load_venues(s, venues)
        await ingest.load_menu_items(s, menus)
        slug = await s.scalar(text("SELECT slug FROM venue WHERE place_id = 'p0'"))
        vid = await s.scalar(text("SELECT id FROM venue WHERE place_id = 'p0'"))
    return {"slug": slug, "venue_id": str(vid)}


# --- venue card --------------------------------------------------------------


async def test_a_venue_card_resolves_by_slug_and_by_id(client, seeded):
    by_slug = await client.get(f"/v1/venues/{seeded['slug']}")
    by_id = await client.get(f"/v1/venues/{seeded['venue_id']}")
    assert by_slug.status_code == by_id.status_code == 200
    assert by_slug.json()["id"] == by_id.json()["id"]


async def test_a_card_carries_the_area_and_the_attribute_confidence(client, seeded):
    body = (await client.get(f"/v1/venues/{seeded['slug']}")).json()
    assert body["area"]["name"] == "Burns Road"
    assert body["attributes"]["dine_in"]["c"] == 0.75
    assert body["phone"] == "+922135870000"


async def test_an_occupancy_with_no_observations_is_labelled_a_prior(client, seeded):
    """The card still shows a number, because "we have no idea" is a worse answer than a
    baseline. It says which it is, and its confidence is capped below anything measured."""
    live = (await client.get(f"/v1/venues/{seeded['slug']}")).json()["live"]
    assert live is not None
    assert live["source"] in {"prior", "archetype"}
    assert live["confidence"] <= 0.45
    assert 0.0 <= live["occupancy"] <= 1.0
    assert live["band"] in {"free", "moderate", "busy", "full"}


async def test_a_google_backed_prior_is_more_confident_than_an_archetype(client, clean_db):
    async with service_session() as s:
        await ingest.load_venues(
            s,
            [
                venue("g", "Measured", popular_times={d: [55] * 24 for d in DAYS}),
                venue("a", "Guessed"),
            ],
        )
        slugs = dict(
            (await s.execute(text("SELECT place_id, slug FROM venue"))).all()
        )
    measured = (await client.get(f"/v1/venues/{slugs['g']}")).json()["live"]
    guessed = (await client.get(f"/v1/venues/{slugs['a']}")).json()["live"]
    assert measured["source"] == "prior"
    assert guessed["source"] == "archetype"
    assert measured["confidence"] > guessed["confidence"]


async def test_an_unknown_venue_is_404(client, seeded):
    assert (await client.get("/v1/venues/no-such-venue")).status_code == 404


async def test_a_hidden_venue_is_not_readable(client, seeded):
    """RLS, not the endpoint. `venue_public_read` filters `status = 'hidden'`."""
    async with service_session() as s:
        await s.execute(
            text("UPDATE venue SET status = 'hidden' WHERE place_id = 'p0'")
        )
    assert (await client.get(f"/v1/venues/{seeded['slug']}")).status_code == 404


# --- venue dishes ------------------------------------------------------------


async def test_dishes_carry_the_printed_name_and_the_family(client, seeded):
    body = (await client.get(f"/v1/venues/{seeded['venue_id']}/dishes")).json()
    assert body["count"] == 1
    dish = body["dishes"][0]
    assert dish["menu_name"] == "Bihari Boti"  # as printed
    assert dish["family"] == "bihari boti"  # what the comparison joins on
    assert dish["price_pkr"] == 700
    assert dish["price_age_days"] is not None


async def test_a_venue_with_no_menu_returns_an_empty_list_not_an_error(client, seeded):
    async with service_session() as s:
        vid = await s.scalar(text("SELECT id FROM venue WHERE place_id = 'p-quiet'"))
    body = (await client.get(f"/v1/venues/{vid}/dishes")).json()
    assert body["count"] == 0
    assert body["dishes"] == []


# --- price comparison --------------------------------------------------------


async def test_the_comparison_gathers_every_spelling_into_one_answer(client, seeded):
    """The whole point of `family`. Five venues, five spellings, one median."""
    body = (await client.get("/v1/dishes/bihari boti/prices")).json()
    assert body["venue_count"] == 5
    assert body["median_pkr"] == 800
    assert body["min_pkr"] == 600
    assert body["max_pkr"] == 1000


async def test_a_printed_name_works_as_the_lookup_key(client, seeded):
    """A caller holding "Beef Behari Boti" should not have to know the family, and the
    protein in it should narrow the result rather than empty it."""
    body = (await client.get("/v1/dishes/Beef Behari Boti/prices")).json()
    assert body["family"] == "bihari boti"
    assert body["protein"] == "beef"
    assert body["venue_count"] == 1
    assert body["quotes"][0]["price_pkr"] == 900


async def test_beef_and_chicken_are_never_averaged_together(client, seeded):
    """§14: a confidently wrong answer is the failure mode this product exists to avoid."""
    beef = (await client.get("/v1/dishes/bihari boti/prices",
                             params={"protein": "beef"})).json()
    chicken = (await client.get("/v1/dishes/bihari boti/prices",
                                params={"protein": "chicken"})).json()
    assert beef["median_pkr"] == 900
    assert chicken["median_pkr"] == 600
    assert {q["protein"] for q in beef["quotes"]} == {"beef"}


async def test_quotes_are_sorted_and_carry_their_age(client, seeded):
    body = (await client.get("/v1/dishes/bihari boti/prices")).json()
    prices = [q["price_pkr"] for q in body["quotes"]]
    assert prices == sorted(prices)
    assert all(q["price_age_days"] is not None for q in body["quotes"])
    assert all(q["stale"] is False for q in body["quotes"])  # priced last month


async def test_a_stale_price_is_shown_and_flagged_not_dropped(client, seeded):
    """Filtering it away silently would make the median look better sourced than it is."""
    async with service_session() as s:
        await s.execute(
            text(
                "UPDATE venue_dish SET price_seen_at = now() - INTERVAL '200 days' "
                "WHERE venue_id = (SELECT id FROM venue WHERE place_id = 'p0')"
            )
        )
    body = (await client.get("/v1/dishes/bihari boti/prices")).json()
    assert body["venue_count"] == 5  # still there
    stale = [q for q in body["quotes"] if q["stale"]]
    assert len(stale) == 1
    assert stale[0]["price_age_days"] > body["stale_after_days"]


async def test_a_thin_sample_says_so(client, seeded):
    body = (await client.get("/v1/dishes/bihari boti/prices",
                             params={"protein": "beef"})).json()
    assert body["venue_count"] == 1
    assert "indicative" in body["note"]


async def test_a_dish_nobody_prices_explains_itself(client, seeded):
    body = (await client.get("/v1/dishes/haleem/prices")).json()
    assert body["venue_count"] == 0
    assert body["quotes"] == []
    assert "menu coverage" in body["note"].lower()


async def test_the_comparison_can_be_narrowed_to_an_area(client, seeded):
    async with service_session() as s:
        area_id = await s.scalar(text("SELECT id FROM area WHERE name = 'Clifton'"))
    body = (await client.get("/v1/dishes/bihari boti/prices",
                             params={"area_id": area_id})).json()
    assert body["venue_count"] == 0


async def test_search_finds_a_dish_through_a_misspelling(client, seeded):
    """The alias work is what makes "behari" reach "bihari boti"."""
    rows = (await client.get("/v1/dishes/search", params={"q": "behari"})).json()
    assert any(r["family"] == "bihari boti" for r in rows)


# --- city --------------------------------------------------------------------


async def test_city_pulse_aggregates_by_area(client, seeded):
    body = (await client.get("/v1/city/pulse")).json()
    names = {a["name"] for a in body["areas"]}
    assert {"Burns Road", "Clifton"} <= names
    burns = next(a for a in body["areas"] if a["name"] == "Burns Road")
    assert burns["venue_count"] == 6
    assert 0.0 <= burns["occupancy_mean"] <= 1.0


async def test_the_map_says_how_much_of_it_is_measured(client, seeded):
    """Every venue here is on a prior, so the honest answer is zero and the API gives it."""
    body = (await client.get("/v1/city/pulse")).json()
    assert body["live_fraction"] == 0.0
    assert all(a["live_venue_count"] == 0 for a in body["areas"])


async def test_city_stats_reports_utilisation_and_the_extremes(client, seeded):
    body = (await client.get("/v1/city/stats")).json()
    assert body["venue_count"] == 7
    assert 0.0 <= body["utilisation_now"] <= 1.0
    assert 0.0 <= body["weekly_mean_utilisation"] <= 1.0
    assert body["live_fraction"] == 0.0


async def test_idle_seats_is_null_rather_than_a_guess(client, seeded):
    """No scraped venue reports its capacity, so scaling an assumed average across the city
    would be inventing the one number an owner would check first."""
    body = (await client.get("/v1/city/stats")).json()
    assert body["idle_seats_now"] is None


async def test_city_endpoints_404_for_a_city_that_is_not_seeded(client, seeded):
    assert (await client.get("/v1/city/pulse", params={"city": "Lahore"})).status_code == 404
    assert (await client.get("/v1/city/stats", params={"city": "Lahore"})).status_code == 404


# --- these are public --------------------------------------------------------


async def test_discovery_never_requires_a_login(client, seeded):
    """Plan §5: `anon` reads public data. Gating search behind an account is the thing every
    competitor does and the reason nobody uses them."""
    for path in ("/v1/city/pulse", "/v1/city/stats", "/v1/dishes/bihari boti/prices",
                 f"/v1/venues/{seeded['slug']}", f"/v1/venues/{seeded['venue_id']}/dishes"):
        assert (await client.get(path)).status_code == 200, path


async def test_ingest_requires_an_admin(client, seeded):
    r = await client.post("/v1/admin/ingest/venues", json={"venues": []})
    assert r.status_code == 401


# --- the clock ---------------------------------------------------------------


async def test_hour_of_week_is_the_venues_hour_not_the_servers():
    """Karachi is UTC+5. Deriving this from the server clock asks for the three-in-the-
    afternoon row at eight in the evening, and returns a number that looks entirely normal."""
    import datetime as dt

    from haazir.services.clock import hour_of_week

    # 20:30 Karachi on a Friday is 15:30 UTC the same day.
    utc = dt.datetime(2026, 9, 4, 15, 30, tzinfo=dt.UTC)
    assert utc.astimezone(dt.timezone(dt.timedelta(hours=5))).hour == 20
    # weekday 5 = Friday under the stored 0 = Sunday convention.
    assert hour_of_week(utc) == 5 * 24 + 20
    assert hour_of_week(utc) != 5 * 24 + 15  # what the naive version would have said


async def test_hour_of_week_covers_the_whole_week():
    import datetime as dt

    from haazir.services.clock import hour_of_week

    start = dt.datetime(2026, 9, 6, 0, 0, tzinfo=dt.UTC)  # a Sunday
    seen = {hour_of_week(start + dt.timedelta(hours=h)) for h in range(168)}
    assert seen == set(range(168))


async def test_the_sql_and_python_clocks_agree(client, seeded):
    """One is used by the city aggregate and the other by the venue card. If they drift, the
    map and the card disagree about what hour it is and nothing raises."""
    from sqlalchemy import text

    from haazir.services.clock import HOUR_OF_WEEK_SQL, hour_of_week, local_now

    async with service_session() as s:
        from_sql = await s.scalar(
            text(f"SELECT {HOUR_OF_WEEK_SQL} FROM city c WHERE c.name = 'Karachi'")
        )
    assert from_sql == hour_of_week(local_now())
