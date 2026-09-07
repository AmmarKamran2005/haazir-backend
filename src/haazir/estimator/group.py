"""The group solver. Port of `solveGroup` in `app/assets/js/engine.js`. Plan §6.6.

Six friends, six sets of constraints, one restaurant. The objective maximises the **minimum**
weighted satisfaction rather than the mean, so the answer protects whoever the venue suits
worst instead of averaging them away. That is the whole product claim on this surface: the
person who always gives way is the reason group decisions quietly stop happening.

**Why this is not OR-Tools, which the plan names.** CP-SAT earns its place when many decision
variables constrain one another. Here there is exactly one decision — which venue — with a
domain of a few hundred candidates and no coupling between them, because each member's
feasibility is a filter on that domain rather than a relation to another variable. Enumerating
the domain is not an approximation of what a solver would do; it *is* the optimum, in about
twenty lines and a millisecond, with no hundred-megabyte dependency in the image. If a later
phase needs to seat a group across several tables, or choose a time slot and a venue together,
that is a genuine constraint problem and CP-SAT should come back with it.

**Two directions that are easy to get backwards, and were.**

A carry-over weight amplifies a member's *shortfall*, never their satisfaction. Multiplying
`u` by 1.4 pushes the compromised member above everyone else and stops them being the binding
minimum, removing the exact protection the weight exists to provide.

Budget and travel *satisfice*, they do not maximise. Nobody's evening is improved by the
restaurant being cheaper than they were willing to pay; they need it inside the ceiling.
Treating those as maximands returns the cheapest venue in the city every single time.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import queueing, travel

# The objective: mostly max-min, lightly regularised by the mean so that among options with
# the same worst-served member, the one that is better for everybody else wins.
MIN_WEIGHT = 0.72
MEAN_WEIGHT = 0.28

# Per-member utility terms. These sum to 1.0 across a member who scores full marks everywhere.
W_BUDGET, W_TRAVEL, W_WAIT, W_MOOD, W_DISH, W_TRUST = 0.22, 0.16, 0.16, 0.22, 0.14, 0.10

# How far inside a ceiling counts as fully satisfied. Below 75% of budget and 55% of the
# travel limit, more headroom adds nothing.
BUDGET_COMFY = 0.75
TRAVEL_COMFY = 0.55

CANDIDATE_LIMIT = 300


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def satisfice(x: float, limit: float, comfy: float) -> float:
    """1.0 well inside the ceiling, falling to 0 at it. Not a maximand."""
    if limit <= 0:
        return 1.0
    if x <= limit * comfy:
        return 1.0
    return clamp(1.0 - (x - limit * comfy) / (limit * (1.0 - comfy)), 0.0, 1.0)


@dataclass(frozen=True, slots=True)
class Member:
    """One person's private inputs. Never returned to anybody, including the group creator."""

    slot: int
    name: str
    budget_pkr: int | None = None
    max_travel_min: int | None = None
    diet: list[str] = field(default_factory=list)
    mood: str | None = None
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class Satisfaction:
    """What a member got. A utility and a name; never a budget, a limit or a restriction."""

    slot: int
    name: str
    u: float

    def as_dict(self) -> dict:
        return {"slot": self.slot, "name": self.name, "u": round(self.u, 4)}


@dataclass(slots=True)
class Solution:
    venue_id: uuid.UUID
    venue_name: str
    area: str | None
    objective: float
    min_sat: float
    mean_sat: float
    satisfaction: list[Satisfaction]
    travel_min: float
    occupancy: float
    band: str
    wait_p50: float


def member_utility(member: Member, venue: dict, travel_min: float) -> float | None:
    """`None` means infeasible. A violated hard constraint is never a penalty (§14 rule 2)."""
    # --- feasibility --------------------------------------------------------
    if member.max_travel_min is not None and travel_min > member.max_travel_min:
        return None
    ticket = venue.get("avg_ticket_pkr")
    if member.budget_pkr is not None and ticket and ticket > member.budget_pkr:
        return None

    attributes = venue.get("attributes") or {}
    for restriction in member.diet:
        if restriction == "nut_allergy":
            transparent = bool(attributes.get("kitchen_transparency", {}).get("v"))
            if not transparent and (venue.get("trust_score") or 0) < 70:
                return None
        elif restriction == "no_beef" and not venue.get("has_non_beef_dish", True):
            return None
        elif restriction == "vegetarian":
            veg = bool(attributes.get("serves_vegetarian", {}).get("v"))
            if not veg and not venue.get("has_vegetarian_dish", False):
                return None
        elif restriction == "halal":
            halal = attributes.get("halal_certified", {}).get("v")
            if halal is False:
                return None

    # --- preference ---------------------------------------------------------
    u = 0.0

    if member.budget_pkr and ticket:
        u += W_BUDGET * satisfice(float(ticket), float(member.budget_pkr), BUDGET_COMFY)
    else:
        # An unknown price is neither a bargain nor a warning. Scoring it as either would
        # decide the evening on a field the scraper happened not to fill.
        u += W_BUDGET * 0.6

    if member.max_travel_min:
        u += W_TRAVEL * satisfice(travel_min, float(member.max_travel_min), TRAVEL_COMFY)
    else:
        u += W_TRAVEL * clamp(1.0 - travel_min / 45.0, 0.0, 1.0)

    u += W_WAIT * clamp(1.0 - float(venue.get("wait_p50") or 0.0) / 45.0, 0.0, 1.0)

    cuisines = {c.lower() for c in (venue.get("cuisines") or [])}
    match member.mood:
        case "bbq":
            u += W_MOOD if "bbq" in cuisines else 0.05
        case "spicy":
            u += W_MOOD * 0.9 if {"bbq", "pakistani", "nihari"} & cuisines else 0.06
        case "quiet":
            quiet = bool(attributes.get("outdoor_seating", {}).get("v"))
            u += W_MOOD if quiet else 0.04
        case "seafood":
            u += W_MOOD if "seafood" in cuisines else 0.05
        case _:
            # No stated mood is not a complaint. A member who did not express one should not
            # drag the group's minimum down for it.
            u += W_MOOD * 0.64

    quality = venue.get("best_dish_quality")
    u += W_DISH * (clamp(float(quality) / 10.0, 0.0, 1.0) if quality else 0.5)
    u += W_TRUST * clamp((venue.get("trust_score") or 55) / 100.0, 0.0, 1.0)

    return clamp(u, 0.0, 1.0)


def score_venue(venue: dict, members: list[Member], travel_min: float) -> dict | None:
    """One venue against the whole group. `None` if anybody cannot go."""
    utilities: list[tuple[Member, float]] = []
    for member in members:
        u = member_utility(member, venue, travel_min)
        if u is None:
            return None
        utilities.append((member, u))

    if not utilities:
        return None

    # The weight amplifies the shortfall. See the module note; the other direction removes
    # the protection it exists to give.
    weighted = [clamp(1.0 - (1.0 - u) * m.weight, 0.0, 1.0) for m, u in utilities]
    raw = [u for _, u in utilities]

    return {
        "min_weighted": min(weighted),
        "min_sat": min(raw),
        "mean_sat": sum(raw) / len(raw),
        "objective": MIN_WEIGHT * min(weighted) + MEAN_WEIGHT * (sum(raw) / len(raw)),
        "satisfaction": [Satisfaction(m.slot, m.name, u) for m, u in utilities],
    }


# --- the query ---------------------------------------------------------------

_CANDIDATES_SQL = """
WITH here AS (
    SELECT ST_SetSRID(ST_MakePoint(:from_lng, :from_lat), 4326)::geography AS g
)
SELECT v.id, v.name, v.cuisines, v.attributes, v.avg_ticket_pkr, v.capacity_covers,
       a.name AS area,
       t.score AS trust_score,
       COALESCE(l.occupancy, p.mean_ratio) AS occupancy,
       COALESCE(l.sd, p.sigma)             AS sd,
       l.wait_p50_min,
       {travel} AS travel_min,
       (SELECT max(vd.quality_mean) FROM venue_dish vd WHERE vd.venue_id = v.id)
           AS best_dish_quality,
       EXISTS (SELECT 1 FROM venue_dish vd JOIN dish d ON d.id = vd.dish_id
                WHERE vd.venue_id = v.id AND d.protein IS DISTINCT FROM 'beef')
           AS has_non_beef_dish,
       EXISTS (SELECT 1 FROM venue_dish vd JOIN dish d ON d.id = vd.dish_id
                WHERE vd.venue_id = v.id
                  AND ('vegetarian' = ANY(d.dietary_flags) OR d.protein = 'vegetarian'))
           AS has_vegetarian_dish
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
 WHERE v.status = 'active'
   AND c.name = :city
   AND COALESCE(l.occupancy, p.mean_ratio) IS NOT NULL
   AND ST_DWithin(v.geom, here.g, :radius_m)
 ORDER BY COALESCE(t.score, 55) DESC, v.google_review_count DESC NULLS LAST
 LIMIT :limit
"""


async def solve(
    session: AsyncSession,
    members: list[Member],
    *,
    from_lat: float,
    from_lng: float,
    city: str = "Karachi",
    now: dt.datetime | None = None,
) -> tuple[list[Solution], dict[str, Any]]:
    """Rank every feasible venue for this group. Returns `(solutions, diagnostics)`.

    The caller gets solutions carrying a satisfaction vector and nothing else. Diagnostics
    say how many venues were ruled out and by which constraint *kind* — never by which member,
    because "Ayesha's budget excluded eleven places" is exactly the disclosure this surface
    exists to prevent.
    """
    now = now or dt.datetime.now(dt.UTC)
    hour = now.hour + now.minute / 60.0

    # The radius has to cover the most generous member; anyone tighter is filtered per-member
    # inside `member_utility`.
    limits = [m.max_travel_min for m in members if m.max_travel_min]
    widest = max(limits) if limits else 45
    congestion = travel.congestion_factor(hour)
    radius_m = max(1.0, widest - travel.OVERHEAD_MIN) * travel.FREE_FLOW_KMH / (
        60.0 * max(congestion, 1.0) * 1.35
    ) * 1000.0 * 1.3

    rows = (
        await session.execute(
            text(_CANDIDATES_SQL.format(travel=travel.TRAVEL_MINUTES_SQL)),
            {
                "from_lat": from_lat, "from_lng": from_lng, "city": city,
                "congestion": congestion, "radius_m": radius_m, "limit": CANDIDATE_LIMIT,
            },
        )
    ).mappings().all()

    solutions: list[Solution] = []
    infeasible = 0

    for row in rows:
        venue = dict(row)
        occupancy = float(venue["occupancy"])
        wait = (
            float(venue["wait_p50_min"])
            if venue["wait_p50_min"] is not None
            else queueing.wait_at(occupancy, venue["capacity_covers"])
        )
        venue["wait_p50"] = wait
        travel_min = float(venue["travel_min"] or 20)

        scored = score_venue(venue, members, travel_min)
        if scored is None:
            infeasible += 1
            continue

        solutions.append(
            Solution(
                venue_id=venue["id"], venue_name=venue["name"], area=venue["area"],
                objective=scored["objective"], min_sat=scored["min_sat"],
                mean_sat=scored["mean_sat"], satisfaction=scored["satisfaction"],
                travel_min=travel_min, occupancy=occupancy,
                band=queueing.state_band(occupancy), wait_p50=wait,
            )
        )

    solutions.sort(key=lambda s: -s.objective)

    diagnostics = {
        "candidates": len(rows),
        "feasible": len(solutions),
        "ruled_out": infeasible,
        # A COUNT of dietary requirements in play, never their names. An earlier version
        # listed them, reasoning that naming the kind without the person was harmless. In a
        # group of two it identifies the other person outright, and in a group of six it
        # still discloses health information that its owner gave to this product and not to
        # their friends. The organiser needs to know a constraint is binding, not which.
        "dietary_constraints": len({d for m in members for d in m.diet}),
        "responded": len(members),
    }
    return solutions, diagnostics


async def load_members(session: AsyncSession, group_id: uuid.UUID) -> list[Member]:
    """Read every member's constraint.

    Only reachable from `solver_session()`, which sets `app.solver`. That is the one policy on
    `group_constraint` that returns more than a single row, and the reason this function
    returns `Member` objects that the router turns into utilities and then discards: nothing
    above this line ever sees a budget.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT m.slot, m.display_name, m.weight,
                       gc.budget_pkr, gc.max_travel_min, gc.diet, gc.mood
                  FROM group_member m
                  JOIN group_constraint gc
                    ON gc.group_id = m.group_id AND gc.member_slot = m.slot
                 WHERE m.group_id = :g
                 ORDER BY m.slot
                """
            ),
            {"g": group_id},
        )
    ).mappings().all()

    return [
        Member(
            slot=r["slot"], name=r["display_name"], budget_pkr=r["budget_pkr"],
            max_travel_min=r["max_travel_min"], diet=list(r["diet"] or []),
            mood=r["mood"], weight=float(r["weight"] or 1.0),
        )
        for r in rows
    ]
