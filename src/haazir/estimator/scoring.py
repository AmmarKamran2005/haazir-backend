"""Ranking. Port of `scoreVenue`, `hardFilter`, `search` and `frontier` in
`app/assets/js/engine.js`. Plan §6.4.

**The score is a named linear combination and the API returns its terms.** Not because that is
tidy, but because the explanation a diner reads has to be a read-out of the arithmetic rather
than a story generated about it. If the weights ever stop being visible, the product is making
an unfalsifiable claim, which is the thing every competitor already does.

**Hard constraints are filters, applied before scoring, never penalties inside it.** A nut
allergy is not a preference that a high enough taste score can outvote. §14 rule 2, and
Phase 4's acceptance criterion says it must be *provable*: the filters run in SQL, so a venue
that fails one is not in the candidate set at all and no amount of scoring can bring it back.

**Everything scoreable arrives in one query.** The prototype loops over 29 venues in a
browser. At 1,700 venues, per-venue round trips to Singapore would be a minute of latency, so
the filter and the gather are one statement and the scoring is pure arithmetic on the result.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import queueing, travel
from . import fusion
from .fusion import SIGMA_REF

# Shown to the user, and summing to 1.0 is what makes the read-out honest.
WEIGHTS: dict[str, float] = {
    "palate": 0.30,
    "live": 0.24,
    "value": 0.18,
    "trust": 0.16,
    "travel": 0.12,
}

# 12% of slack over a stated budget, for drinks and tax. Beyond that a typed ceiling is a
# ceiling: "under Rs 2,500" returning a Rs 5,800 restaurant makes the whole counterfactual
# panel pointless, because there is nothing left for it to offer.
BUDGET_SLACK = 1.12

DEFAULT_MAX_TRAVEL = 40
DEFAULT_BUDGET = 2500

# What an access fact must reach before it counts as satisfied. A fact nobody has confirmed is
# not a "yes" for someone who needs a ramp to get through the door.
FACT_CONFIDENCE_FLOOR = 0.5


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass(slots=True)
class Query:
    text: str | None = None
    from_lat: float | None = None
    from_lng: float | None = None
    party: int = 1
    budget: int | None = None
    max_travel: int | None = None
    mood: str | None = None
    cuisine: str | None = None
    dish: str | None = None
    needs_prayer: bool = False
    needs_family: bool = False
    needs_ramp: bool = False
    needs_card: bool = False
    diet: list[str] = field(default_factory=list)
    open_now: bool = False
    limit: int = 20
    city: str = "Karachi"


@dataclass(slots=True)
class Scored:
    venue_id: uuid.UUID
    slug: str
    name: str
    area: str | None
    total: float
    factors: dict[str, float]
    occupancy: float
    band: str
    confidence: float
    occupancy_source: str
    wait_p50: float
    wait_p90: float
    travel_min: float
    spend: int | None
    trust_score: int | None
    # Carried so a result can be rendered without a second request per row. `avg_ticket_pkr`
    # is null on every scraped venue — Google publishes a price level, not a ticket — so
    # `price_level` is what a caller can actually show, and `spend` stays null rather than
    # becoming a guess.
    cuisines: list[str] = field(default_factory=list)
    price_level: int | None = None
    # Which signals produced the estimate. live_state is already joined, so this costs
    # nothing, and without it a caller can only say "fusion of 0 sources" — which reads as
    # "we have nothing" when what is true is "the prior is carrying it".
    source_weights: dict = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "venue_id": str(self.venue_id),
            "slug": self.slug,
            "name": self.name,
            "area": self.area,
            "score": round(self.total, 4),
            # The terms, not just the total. This is the explanation.
            "factors": {k: round(v, 4) for k, v in self.factors.items()},
            "weights": WEIGHTS,
            "live": {
                "occupancy": round(self.occupancy, 4),
                "band": self.band,
                "confidence": round(self.confidence, 3),
                "source": self.occupancy_source,
                "wait_p50_min": round(self.wait_p50, 1),
                "wait_p90_min": round(self.wait_p90, 1),
                "sources": fusion.source_lines(self.source_weights),
            },
            "travel_min": round(self.travel_min),
            "expected_spend_pkr": self.spend,
            "trust_score": self.trust_score,
            "cuisines": self.cuisines,
            "price_level": self.price_level,
        }


# --- hard filters ------------------------------------------------------------


def _fact_true(attributes: dict, key: str) -> bool:
    """An access fact counts as satisfied only if it is recorded AND believed.

    `{"v": true, "c": 0.2}` is somebody's unconfirmed guess. Treating that as a yes for a
    wheelchair user is how an app sends someone to a restaurant they cannot enter.
    """
    fact = (attributes or {}).get(key)
    if not isinstance(fact, dict):
        return False
    return bool(fact.get("v")) and float(fact.get("c", 0)) >= FACT_CONFIDENCE_FLOOR


def hard_filter_sql(q: Query, params: dict) -> list[str]:
    """The filters, as SQL. Returns the WHERE fragments and fills `params`.

    In SQL rather than Python so that a venue failing a constraint never enters the candidate
    set. That is what makes Phase 4's "provably filters" criterion checkable: there is no
    later stage that could re-admit it.
    """
    where = ["v.status = 'active'", "c.name = :city"]
    params["city"] = q.city

    if q.budget:
        # NULL avg_ticket is not silently dropped: a venue whose price nobody knows is still
        # a candidate, because excluding it would quietly shrink the city to the venues that
        # happen to have been priced.
        where.append("(v.avg_ticket_pkr IS NULL OR v.avg_ticket_pkr <= :budget_ceiling)")
        params["budget_ceiling"] = int(q.budget * BUDGET_SLACK)

    if q.cuisine:
        where.append("v.cuisines @> ARRAY[:cuisine]::text[]")
        params["cuisine"] = q.cuisine

    if q.dish:
        where.append(
            "EXISTS (SELECT 1 FROM venue_dish vd JOIN dish d ON d.id = vd.dish_id "
            "         WHERE vd.venue_id = v.id AND d.family = :dish_family)"
        )
        params["dish_family"] = q.dish

    # Access facts. Each is `attributes -> key -> v = true AND c >= floor`, so an unconfirmed
    # guess never satisfies a stated need.
    for flag, key in (
        (q.needs_prayer, "prayer_area"),
        (q.needs_family, "family_section"),
        (q.needs_ramp, "wheelchair_accessible"),
        (q.needs_card, "accepts_cards"),
    ):
        if flag:
            where.append(
                f"(v.attributes -> '{key}' ->> 'v') = 'true' "
                f"AND COALESCE((v.attributes -> '{key}' ->> 'c')::float, 0) >= :fact_floor"
            )
            params["fact_floor"] = FACT_CONFIDENCE_FLOOR

    # Diet. These are the constraints Phase 4's acceptance criterion names, and they are the
    # reason the whole filter runs in SQL: a venue that cannot serve someone safely must not
    # be in the candidate set, where a high enough taste score could still float it to the top.
    for restriction in q.diet:
        match restriction:
            case "nut_allergy":
                # There is no "nut free" attribute to read, and inventing one would be worse
                # than useless. What can be checked is whether the kitchen is inspectable and
                # the venue has a trust record worth relying on, which is the same rule the
                # prototype's group solver applies. A venue that fails it is not condemned;
                # it is simply not somewhere this product will send someone with an allergy
                # on its own recommendation.
                where.append(
                    "((v.attributes -> 'kitchen_transparency' ->> 'v') = 'true' "
                    " OR COALESCE(t.score, 0) >= :allergy_trust)"
                )
                params["allergy_trust"] = 70
            case "no_beef":
                where.append(
                    "EXISTS (SELECT 1 FROM venue_dish vd2 JOIN dish d2 ON d2.id = vd2.dish_id "
                    "         WHERE vd2.venue_id = v.id "
                    "           AND NOT (d2.protein IS NOT DISTINCT FROM 'beef') "
                    "           AND NOT ('contains_beef' = ANY(d2.dietary_flags)))"
                )
            case "vegetarian":
                where.append(
                    "(EXISTS (SELECT 1 FROM venue_dish vd3 JOIN dish d3 ON d3.id = vd3.dish_id "
                    "          WHERE vd3.venue_id = v.id "
                    "            AND ('vegetarian' = ANY(d3.dietary_flags) "
                    "                 OR d3.protein = 'vegetarian')) "
                    " OR (v.attributes -> 'serves_vegetarian' ->> 'v') = 'true')"
                )
            case "halal":
                where.append(
                    "((v.attributes -> 'halal_certified' ->> 'v') = 'true' "
                    " OR (v.attributes -> 'halal' ->> 'v') = 'true')"
                )

    return where


# --- scoring -----------------------------------------------------------------


def score_row(row: dict, q: Query, now: dt.datetime) -> Scored:
    """Five named factors, one weighted sum."""
    occupancy = float(row["occupancy"])
    sd = float(row["sd"])
    capacity = row["capacity_covers"]
    band = queueing.wait_band(occupancy, sd, capacity)
    confidence = clamp(1.0 - sd / SIGMA_REF, 0.0, 0.985)

    # palate — how good the best thing here is right now, nudged by the stated mood.
    best_quality = row["best_dish_quality"]
    palate = (float(best_quality) / 10.0) if best_quality else 0.45
    cuisines = [x.lower() for x in (row["cuisines"] or [])]
    if q.mood == "spicy" and {"bbq", "pakistani", "nihari"} & set(cuisines):
        palate += 0.10
    if q.mood == "bbq" and "bbq" in cuisines:
        palate += 0.12
    if q.mood == "quiet" and _fact_true(row["attributes"], "outdoor_seating"):
        palate += 0.06
    palate = clamp(palate, 0.0, 1.0)

    # live — a short wait and a confident estimate both count, because a five-minute wait
    # nobody can vouch for is not worth the same as one that three sources agree on.
    wait_penalty = clamp(1.0 - band["p50"] / 45.0, 0.0, 1.0)
    live = clamp(wait_penalty * (0.62 + 0.38 * confidence), 0.0, 1.0)

    # value — expected spend against the stated budget, for the whole table.
    ticket = row["avg_ticket_pkr"]
    budget_per_head = q.budget or DEFAULT_BUDGET
    if ticket:
        spend = int(ticket) * max(1, q.party)
        budget_total = budget_per_head * max(1, q.party)
        value = clamp(
            1.0 - max(0.0, spend - budget_total * 0.75) / (budget_total * 0.9), 0.0, 1.0
        )
    else:
        spend = None
        value = 0.5  # unknown price is neither a bargain nor a warning

    trust_raw = row["trust_score"]
    trust = clamp((trust_raw or 55) / 100.0, 0.0, 1.0)

    travel_min = float(row["travel_min"] or 20)
    travel_factor = clamp(1.0 - travel_min / (q.max_travel or DEFAULT_MAX_TRAVEL), 0.0, 1.0)

    factors = {
        "palate": palate, "live": live, "value": value,
        "trust": trust, "travel": travel_factor,
    }
    total = sum(WEIGHTS[k] * v for k, v in factors.items())

    return Scored(
        venue_id=row["id"], slug=row["slug"], name=row["name"], area=row["area_name"],
        total=total, factors=factors,
        occupancy=occupancy, band=queueing.state_band(occupancy), confidence=confidence,
        occupancy_source=row["occupancy_source"],
        wait_p50=band["p50"], wait_p90=band["p90"],
        travel_min=travel_min, spend=spend, trust_score=trust_raw,
        cuisines=list(row["cuisines"] or []), price_level=row["price_level"],
        source_weights=row["source_weights"] or {},
    )


# --- the query ---------------------------------------------------------------

_CANDIDATE_SQL = """
WITH here AS (
    SELECT ST_SetSRID(ST_MakePoint(:from_lng, :from_lat), 4326)::geography AS g
)
SELECT v.id, v.slug, v.name, v.cuisines, v.attributes, v.avg_ticket_pkr,
       v.price_level, v.capacity_covers, v.google_rating,
       a.name AS area_name,
       t.score AS trust_score,
       l.source_weights,
       COALESCE(l.occupancy, p.mean_ratio) AS occupancy,
       COALESCE(l.sd, p.sigma)             AS sd,
       -- 'live' means an observation contributed, not merely that a live_state row exists.
       -- The old test (l.occupancy IS NOT NULL) called every venue live and contradicted the
       -- source list beside it, and that false fact was what the explanation model was given.
       CASE WHEN {is_live} THEN 'live'
            WHEN p.source = 'google_popular_times' THEN 'prior'
            ELSE 'archetype' END           AS occupancy_source,
       {travel} AS travel_min,
       (SELECT max(vd.quality_mean) FROM venue_dish vd WHERE vd.venue_id = v.id)
                                           AS best_dish_quality
  FROM venue v
  JOIN city c ON c.id = v.city_id
  CROSS JOIN here
  LEFT JOIN area a ON a.id = v.area_id
  LEFT JOIN trust_score t ON t.venue_id = v.id
  LEFT JOIN live_state l ON l.venue_id = v.id
  LEFT JOIN occupancy_prior p
         ON p.venue_id = v.id
        AND p.hour_of_week = (EXTRACT(DOW  FROM now() AT TIME ZONE c.timezone)::int * 24
                            + EXTRACT(HOUR FROM now() AT TIME ZONE c.timezone)::int)
 WHERE {where}
   AND COALESCE(l.occupancy, p.mean_ratio) IS NOT NULL
   -- ST_DWithin can use venue_geom_gix; ST_Distance in a WHERE clause cannot. At Karachi's
   -- 1,700 venues the planner still picks a sequential scan and is right to, so this buys
   -- nothing measurable today. It is here for the plan's stated next step, the rest of
   -- Pakistan, where scanning every venue to compute a distance and discard it stops being
   -- free. The radius is deliberately generous: it can only admit rows the exact filter
   -- below then rejects, never exclude one it would have kept.
   AND ST_DWithin(v.geom, here.g, :radius_m)
   AND {travel} <= :max_travel
 ORDER BY COALESCE(t.score, 55) DESC, v.google_review_count DESC NULLS LAST
 LIMIT :candidate_limit
"""

# Rank a bounded candidate set rather than the whole city. Ordered by trust and review volume
# so the cut is defensible: what falls off the end is the long tail nobody has an opinion
# about, not a well-regarded venue that happened to sort late.
CANDIDATE_LIMIT = 400


def _radius_for(max_travel_min: float, congestion: float) -> float:
    """Metres that `max_travel_min` could possibly cover, generously.

    The inverse of the travel formula, then 30% on top. Over-estimating costs a few extra
    rows that the exact filter drops; under-estimating would silently hide a restaurant that
    is genuinely within reach, which is a wrong answer nobody could see.
    """
    usable = max(1.0, max_travel_min - travel.OVERHEAD_MIN)
    km = usable * travel.FREE_FLOW_KMH / (60.0 * max(congestion, 1.0) * 1.35)
    return km * 1000.0 * 1.3


async def search(session: AsyncSession, q: Query, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.UTC)
    hour = now.hour + now.minute / 60.0

    max_travel = q.max_travel or DEFAULT_MAX_TRAVEL
    congestion = travel.congestion_factor(hour)
    params: dict = {
        "from_lat": q.from_lat if q.from_lat is not None else 24.8607,
        "from_lng": q.from_lng if q.from_lng is not None else 67.0011,
        "congestion": congestion,
        "max_travel": max_travel,
        "radius_m": _radius_for(max_travel, congestion),
        "candidate_limit": CANDIDATE_LIMIT,
    }
    where = hard_filter_sql(q, params)

    sql = _CANDIDATE_SQL.format(
        travel=travel.TRAVEL_MINUTES_SQL,
        where=" AND ".join(where),
        is_live=fusion.SOURCE_IS_LIVE_SQL,
    )
    rows = (await session.execute(text(sql), params)).mappings().all()

    scored = sorted(
        (score_row(dict(r), q, now) for r in rows), key=lambda s: -s.total
    )
    return {
        "results": scored[: q.limit],
        "candidates": len(rows),
        "relaxed": False,
    }


async def nearest_match_minutes(
    session: AsyncSession, q: Query, now: dt.datetime | None = None
) -> float | None:
    """Travel time to the closest venue that satisfies everything EXCEPT travel.

    Used to relax by the right amount instead of an arbitrary one. Adding a fixed twenty
    minutes reaches nothing when the nearest match is forty minutes out, which puts the API
    back where it started with an empty list.
    """
    now = now or dt.datetime.now(dt.UTC)
    hour = now.hour + now.minute / 60.0
    params: dict = {
        "from_lat": q.from_lat if q.from_lat is not None else 24.8607,
        "from_lng": q.from_lng if q.from_lng is not None else 67.0011,
        "congestion": travel.congestion_factor(hour),
    }
    where = hard_filter_sql(q, params)
    sql = f"""
        SELECT MIN({travel.TRAVEL_MINUTES_SQL})
          FROM venue v
          JOIN city c ON c.id = v.city_id
          LEFT JOIN trust_score t ON t.venue_id = v.id
         WHERE {" AND ".join(where)}
    """
    return await session.scalar(text(sql), params)


async def search_with_relaxation(
    session: AsyncSession, q: Query, now: dt.datetime | None = None
) -> dict:
    """Never return an empty result set for a reason the diner could have changed.

    The frontend contract (`docs/FRONTEND-PLAN.md`) is that an empty list is not a state the
    UI renders. So when nothing matches, travel is relaxed to whatever actually reaches the
    nearest qualifying venue and the response says by how much.

    Travel goes first because it is the one constraint a diner can trade away by leaving
    earlier. A budget is relaxed only as a second resort, and a dietary or access requirement
    is never relaxed at all: an empty list is the correct answer to "somewhere safe for a nut
    allergy" when there is nowhere safe, and quietly widening it would be the single worst
    thing this product could do.
    """
    result = await search(session, q, now)
    if result["results"]:
        return result

    asked = q.max_travel or DEFAULT_MAX_TRAVEL
    nearest = await nearest_match_minutes(session, q, now)
    if nearest is not None and nearest > asked:
        reach = int(nearest) + 2
        widened = dataclasses.replace(q, max_travel=reach)
        result = await search(session, widened, now)
        if result["results"]:
            result["relaxed"] = True
            result["relaxed_note"] = (
                f"Nothing matched within {asked} minutes. The nearest place that does is "
                f"about {int(nearest)} minutes away, so this list reaches {reach}."
            )
            return result

    if q.budget:
        raised = int(q.budget * 1.4)
        widened = dataclasses.replace(
            q, budget=raised, max_travel=max(asked, int(nearest or asked) + 2)
        )
        result = await search(session, widened, now)
        if result["results"]:
            result["relaxed"] = True
            result["relaxed_note"] = (
                f"Nothing matched under Rs {q.budget:,} a head, so this list reaches "
                f"Rs {raised:,}."
            )
            return result

    # Genuinely nothing, even relaxed. The list is empty and the response says why, because
    # an empty array with no explanation is the one thing the frontend contract forbids and
    # is also just a worse answer: the caller cannot tell whether the city is closed, the
    # budget is impossible, or something broke.
    result["relaxed"] = False
    result["empty_reason"] = _why_nothing_matched(q)
    return result


def _why_nothing_matched(q: Query) -> str:
    """The most likely binding constraint, said plainly.

    Ordered by which is most often the real cause, and deliberately never blames a dietary or
    access requirement: those are not the caller's to loosen, and implying otherwise would
    read as "your allergy is the problem".
    """
    if q.budget and q.budget < 500:
        total = q.budget * max(1, q.party)
        return (
            f"Rs {total:,} for {q.party} is about Rs {q.budget:,} a head, which is below what "
            f"any venue in this dataset charges. Try a higher total, or fewer people."
        )
    if q.max_travel and q.max_travel <= 15:
        return f"Nothing is within {q.max_travel} minutes. Try widening the travel time."
    if q.diet:
        return (
            "No venue meets all of these requirements together. These are not relaxed "
            "automatically; widening the area is the safest thing to change."
        )
    if q.dish:
        return (
            f"No venue in range has {q.dish!r} on a priced menu. Menu coverage is thin: "
            f"prices come from delivery listings and most venues have none yet."
        )
    return "No venue matched. Try widening the area or the budget."
