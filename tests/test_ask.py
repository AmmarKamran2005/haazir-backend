"""Phase 8 acceptance: intent, templates, and the budget guard. Plan §12, §9.

*"With the LLM key removed the product still works end to end using template explanations;
the budget guard trips and degrades without an error reaching the user."*

The whole suite runs with no `GEMINI_API_KEY`, so every test in this file is already the
first half of that criterion: if any of it needed a model, it would fail here.

The parser tests are the other reason this file exists. Both behaviours the plan singles out
were once bugs, and neither would raise anything if it came back.
"""

from __future__ import annotations

import pytest

from haazir.services import llm
from haazir.services.intent import parse, to_search_query

from .conftest import requires_db

# --- the parser, no database -------------------------------------------------


def test_party_size_is_read_before_budget():
    """"2500 tak, 6 log hain" is Rs 2,500 for the table, not each. Read the other way round,
    the app quietly sextuples the budget and returns restaurants nobody can afford."""
    intent = parse("2500 tak, 6 log hain")
    assert intent.party == 6
    assert intent.budget_total_pkr == 2500
    assert intent.budget_pkr == 417  # 2500 / 6


def test_per_head_is_honoured_when_it_is_said():
    intent = parse("1200 per head, 4 log")
    assert intent.party == 4
    assert intent.budget_pkr == 1200
    assert intent.budget_total_pkr == 4800


def test_abhi_means_now_not_hurry():
    """Reading "abhi" as travel urgency capped this question at 15 minutes and dropped half
    the nihari in the city from something that was about timing, not distance."""
    intent = parse("nihari, abhi sahi hai ya subah aaun?")
    assert intent.max_travel_min == 30  # the default, untouched
    assert intent.dish == "nihari"


def test_jaldi_does_mean_hurry():
    assert parse("jaldi kuch chahiye").max_travel_min == 15
    assert parse("kuch quick batao").max_travel_min == 15


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("kuch teekha", "spicy"),
        ("masaledar chahiye", "spicy"),
        ("bbq karna hai", "bbq"),
        ("seekh kabab", "bbq"),
        ("sukoon wali jagah", "quiet"),
        ("somewhere we can talk", "quiet"),
        ("machli khani hai", "seafood"),
    ],
)
def test_mood_is_recognised_in_roman_urdu(text, expected):
    assert parse(text).mood == expected


@pytest.mark.parametrize(
    ("text", "attribute"),
    [
        ("bachon ke sath", "needs_family"),
        ("ghar walon ke sath", "needs_family"),
        ("namaz ki jagah ho", "needs_prayer"),
        ("wheelchair access chahiye", "needs_ramp"),
        ("card chalta hai?", "needs_card"),
        ("cash nahi hai", "needs_card"),
    ],
)
def test_hard_constraints_are_recognised(text, attribute):
    assert getattr(parse(text), attribute) is True


@pytest.mark.parametrize(
    ("text", "restriction"),
    [
        ("nut allergy hai", "nut_allergy"),
        ("beef nahi khate", "no_beef"),
        ("vegetarian hoon", "vegetarian"),
        ("halal hona chahiye", "halal"),
    ],
)
def test_dietary_restrictions_are_recognised(text, restriction):
    assert restriction in parse(text).diet


def test_sasta_infers_a_budget_but_never_overrides_a_stated_one():
    assert parse("kuch sasta").budget_pkr == 700
    # A number the user actually typed always wins over a word we inferred from.
    assert parse("sasta, 2000 tak").budget_pkr == 2000


def test_a_small_number_is_not_read_as_a_budget():
    """Under Rs 200 is far more likely a time or a table number than a dining budget."""
    intent = parse("11 baje ke baad")
    assert intent.budget_pkr == 1500  # the default, not 11
    assert intent.late is True


def test_the_parser_records_what_it_understood():
    """The audit trail. A wrong result should be traceable to a misreading rather than to a
    model nobody can question."""
    intent = parse("bachon ke sath, 2500 tak, 6 log, koi acha bbq")
    keys = {e.key for e in intent.extracted}
    assert {"Party size", "Budget", "Mood", "Hard constraint"} <= keys
    assert all(e.source == "parser" for e in intent.extracted)


def test_an_unrecognisable_query_says_so_rather_than_guessing():
    """The one case where asking a model is worth the money. Everything else was free."""
    assert parse("kuch bhi").understood is False
    assert parse("2500 tak, 6 log hain").understood is True


def test_the_parser_never_raises():
    for text in ("", "   ", "!!!", "٢٥٠٠", "a" * 280, "😀"):
        assert parse(text) is not None


def test_the_intent_becomes_a_search_query():
    query = to_search_query(parse("behari boti, 1500 tak, family ke sath"))
    assert query.budget == 1500
    assert query.needs_family is True
    # Canonicalised through the same normaliser the ingest used, so the family matches what
    # is actually stored.
    assert query.dish == "bihari boti"


# --- templates, which are the default ----------------------------------------


def result_row(**over) -> dict:
    return {
        "venue_id": "abc", "name": "Kolachi", "area": "Do Darya",
        "expected_spend_pkr": 3600, "travel_min": 22, "trust_score": 82,
        "factors": {"palate": 0.8},
        "live": {"occupancy": 0.83, "band": "busy", "confidence": 0.72,
                 "wait_p50_min": 18, "source": "live"},
    } | over


def test_a_template_explanation_uses_only_the_numbers_it_was_given():
    sentence = llm.explain(result_row(), party=2, from_area="Clifton")
    assert "18 min wait" in sentence
    assert "72% confident" in sentence
    assert "Rs 3,600 for 2 people" in sentence
    assert "22 min from Clifton" in sentence


def test_a_template_says_when_a_number_was_not_measured():
    """§14 rule 1. A modelled figure and a measured one must never read alike."""
    prior = llm.explain(result_row(live={**result_row()["live"], "source": "prior"}))
    archetype = llm.explain(result_row(live={**result_row()["live"], "source": "archetype"}))
    live = llm.explain(result_row())

    assert "not a live reading" in prior
    assert "nobody has reported" in archetype
    assert "not a live reading" not in live


def test_a_short_wait_reads_as_seated_on_arrival():
    quiet = result_row(live={"occupancy": 0.3, "band": "free", "confidence": 0.4,
                             "wait_p50_min": 0, "source": "live"})
    assert "Seated on arrival at 30% full" in llm.explain(quiet)


def test_a_low_trust_score_is_surfaced_not_hidden():
    assert "check the record" in llm.explain(result_row(trust_score=40))
    assert "check the record" not in llm.explain(result_row(trust_score=82))


@pytest.mark.asyncio
async def test_explanations_fall_back_to_templates_with_no_key():
    llm.reset()
    sentence, source = await llm.explain_result(result_row(), party=2)
    assert source == "template"
    assert sentence == llm.explain(result_row(), party=2)


# --- the budget guard --------------------------------------------------------


def test_the_guard_trips_at_ninety_percent(monkeypatch):
    from haazir import config

    llm.reset()
    monkeypatch.setattr(config.settings, "llm_monthly_ceiling_usd", 1.0)
    monkeypatch.setattr(config.settings, "gemini_api_key", "test-key")

    assert llm.available() is True

    # Derived from the configured price, not hardcoded. The first version baked in a token
    # count tuned to Anthropic's rates and silently stopped testing anything when the provider
    # changed: the same 61,000 tokens cost 40x less on Gemini, so the guard never tripped and
    # the assertion failed for a reason that had nothing to do with the guard.
    out_price = llm.PRICE_PER_MTOK[llm.MODEL_EXPLAIN]["out"]
    tokens_for_ninety_cents = int(0.91 / out_price * 1_000_000)

    llm.budget.record(llm.MODEL_EXPLAIN, tokens_in=0, tokens_out=tokens_for_ninety_cents)
    assert llm.budget.fraction >= 0.90
    assert llm.budget.degraded is True
    assert llm.available() is False
    assert llm.budget.degraded_since is not None


@pytest.mark.asyncio
async def test_a_tripped_guard_degrades_without_an_error_reaching_the_user(monkeypatch):
    """The half of the criterion that is about what the user sees: nothing."""
    from haazir import config

    llm.reset()
    monkeypatch.setattr(config.settings, "llm_monthly_ceiling_usd", 1.0)
    monkeypatch.setattr(config.settings, "gemini_api_key", "test-key")
    llm.budget.record(llm.MODEL_EXPLAIN, tokens_in=0, tokens_out=61_000)

    sentence, source = await llm.explain_result(result_row(), party=2)
    assert source == "template"
    assert sentence  # a real sentence, not an error string


def test_spend_resets_with_the_month(monkeypatch):
    llm.reset()
    llm.budget.record(llm.MODEL_EXPLAIN, tokens_in=1000, tokens_out=1000)
    assert llm.budget.spent_usd > 0

    llm.budget.month = (llm.budget.month % 12) + 1  # pretend we rolled over
    assert llm.budget.fraction == 0.0
    assert llm.budget.spent_usd == 0.0


def test_available_is_false_without_a_key():
    llm.reset()
    assert llm.available() is False


# --- the endpoint ------------------------------------------------------------


@requires_db
@pytest.mark.asyncio
async def test_ask_works_end_to_end_without_a_model(client, clean_db):
    """Phase 8's criterion, as one request."""
    from haazir.db import service_session
    from haazir.services import ingest_venues as ingest

    async with service_session() as s:
        await ingest.load_venues(
            s,
            [{
                "place_id": "ask-1", "name": "Burns Road BBQ", "area": "Burns Road",
                "lat": 24.8615, "lng": 67.0180, "venue_type": "restaurant",
                "cuisines": ["Pakistani", "BBQ"], "avg_ticket_pkr": 900,
                "attributes": {"dine_in": True}, "scraped_at": "2026-09-03T12:00:00Z",
            }],
        )

    r = await client.post(
        "/v1/ask",
        json={"text": "6000 tak, 6 log hain, koi acha bbq",
              "from_lat": 24.8615, "from_lng": 67.0180, "from_area": "Burns Road"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["query"]["party"] == 6
    assert body["query"]["budget_total_pkr"] == 6000
    assert body["query"]["budget_pkr"] == 1000  # 6000 for the table, not each
    assert body["query"]["mood"] == "bbq"
    assert body["count"] >= 1
    assert body["explanations"]["source"] == "template"
    assert all(row["why"] for row in body["results"])
    assert all(row["why_source"] == "template" for row in body["results"])


@requires_db
@pytest.mark.asyncio
async def test_ask_returns_the_audit_trail(client, clean_db):
    body = (
        await client.post("/v1/ask", json={"text": "bachon ke sath, namaz ki jagah, 2000 tak"})
    ).json()
    understood = {e["key"]: e["value"] for e in body["query"]["understood"]}
    assert "Budget" in understood
    assert "Hard constraint" in understood
    assert body["query"]["parsed_by"] == "parser"


@requires_db
@pytest.mark.asyncio
async def test_ask_says_the_ranking_is_not_done_by_a_model(client, clean_db):
    """A product that claims its ranking is deterministic should say so in the response that
    carries the ranking."""
    body = (await client.post("/v1/ask", json={"text": "kuch acha"})).json()
    assert "never decides the order" in body["explanations"]["note"]


@requires_db
@pytest.mark.asyncio
async def test_the_parse_endpoint_costs_nothing(client, clean_db):
    body = (await client.get("/v1/ask/parse", params={"text": "jaldi sasta, 3 log"})).json()
    assert body["party"] == 3
    assert body["max_travel_min"] == 15
    assert body["budget_pkr"] == 700


@requires_db
@pytest.mark.asyncio
async def test_the_llm_status_endpoint_is_honest(client, clean_db):
    llm.reset()
    body = (await client.get("/v1/llm/status")).json()
    assert body["key_configured"] is False
    assert body["available"] is False
    assert body["explanations"] == "template"
    assert body["budget"]["spent_usd"] == 0.0


@requires_db
@pytest.mark.asyncio
async def test_a_genuinely_impossible_budget_is_explained_not_left_blank(client, clean_db):
    """Rs 2,500 for six is Rs 417 a head, which no venue in the dataset charges. The right
    answer is an empty list, and an empty list with no explanation is the one thing the
    frontend contract forbids."""
    from haazir.db import service_session
    from haazir.services import ingest_venues as ingest

    async with service_session() as s:
        await ingest.load_venues(
            s,
            [{
                "place_id": "ask-2", "name": "Not Cheap", "area": "Burns Road",
                "lat": 24.8615, "lng": 67.0180, "venue_type": "restaurant",
                "cuisines": ["BBQ"], "avg_ticket_pkr": 900,
                "attributes": {"dine_in": True}, "scraped_at": "2026-09-03T12:00:00Z",
            }],
        )

    body = (
        await client.post(
            "/v1/ask",
            json={"text": "2500 tak, 6 log hain, koi acha bbq",
                  "from_lat": 24.8615, "from_lng": 67.0180},
        )
    ).json()

    assert body["count"] == 0
    assert body["empty_reason"]
    assert "a head" in body["empty_reason"]
