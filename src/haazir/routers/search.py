"""Ranked search and the live panel. Plan §7.

Two rules the responses keep, both from §14.

**Every result carries the terms of its own score.** `factors` and `weights` travel with each
venue so the explanation the interface shows is a read-out of the arithmetic. An LLM may later
turn those numbers into a sentence; it never decides the order.

**The result set is never empty.** `docs/FRONTEND-PLAN.md` is explicit that an empty list is
not a state the UI renders. When nothing matches, the API relaxes the softest constraint,
returns what it found, and sets `relaxed` with a note saying what it did. Travel is relaxed
first because it is the one thing a diner can actually change; an access requirement never is.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..auth.deps import Ctx
from ..estimator import fusion, queueing
from ..estimator.scoring import WEIGHTS, Query, search_with_relaxation
from ..services import llm, lookup
from ..services.clock import HOUR_OF_WEEK_SQL

router = APIRouter(prefix="/v1", tags=["search"])


class SearchIn(BaseModel):
    text: str | None = Field(default=None, max_length=280)
    from_lat: float | None = Field(default=None, ge=-90, le=90)
    from_lng: float | None = Field(default=None, ge=-180, le=180)
    party: int = Field(default=1, ge=1, le=30)
    budget: int | None = Field(default=None, ge=50, le=100_000,
                              description="PKR per head, a ceiling not a preference")
    max_travel: int | None = Field(default=None, ge=5, le=120)
    mood: str | None = Field(default=None, max_length=32)
    cuisine: str | None = Field(default=None, max_length=48)
    dish: str | None = Field(default=None, max_length=64)
    needs_prayer: bool = False
    needs_family: bool = False
    needs_ramp: bool = False
    needs_card: bool = False
    diet: list[str] = Field(default_factory=list, max_length=6,
                            description="nut_allergy | no_beef | vegetarian | halal")
    limit: int = Field(default=20, ge=1, le=50)
    city: str = "Karachi"


@router.post("/search")
async def search(body: SearchIn, ctx: Ctx) -> dict:
    q = Query(
        text=body.text, from_lat=body.from_lat, from_lng=body.from_lng,
        party=body.party, budget=body.budget, max_travel=body.max_travel,
        mood=body.mood, cuisine=body.cuisine, dish=body.dish,
        needs_prayer=body.needs_prayer, needs_family=body.needs_family,
        needs_ramp=body.needs_ramp, needs_card=body.needs_card, diet=body.diet,
        limit=body.limit, city=body.city,
    )
    result = await search_with_relaxation(ctx.session, q)

    # Every row explains itself, from the score's own terms. This is `llm.explain`, which is
    # pure Python and makes no network call — /v1/search stays a fast, deterministic endpoint
    # and no caller has to ask separately why something ranked where it did. /v1/ask is where
    # a model is allowed to reword these; here the template is the answer, not a placeholder.
    rows = []
    for scored in result["results"]:
        row = scored.as_dict()
        row["why"] = llm.explain(row, party=q.party)
        row["why_source"] = "template"
        rows.append(row)

    return {
        "results": rows,
        "count": len(result["results"]),
        "candidates_considered": result["candidates"],
        "relaxed": result.get("relaxed", False),
        "relaxed_note": result.get("relaxed_note"),
        # Present only when the list is empty: what was most likely in
        # the way, so the caller is never left with a bare [].
        "empty_reason": result.get("empty_reason"),
        "weights": WEIGHTS,
        # Said plainly rather than left for the reader to infer from `source` on each row.
        "live_fraction": round(
            sum(1 for r in result["results"] if r.occupancy_source == "live")
            / max(1, len(result["results"])), 3
        ),
    }


@router.get("/venues/{ident}/live")
async def venue_live(ident: str, ctx: Ctx) -> dict:
    """The fused estimate and the reason to believe it.

    Served from `live_state`, which the refresh job keeps current, and NOT by re-fusing the
    raw observations on read. That is a deliberate limit rather than a shortcut: §4 gives
    `observation` no SELECT policy for any user role, because a table of who reported what
    from where is not something a diner should be able to page through. `live_state` is the
    public projection of it, and the per-source weights stored alongside are what make the
    number checkable without exposing the rows behind it.

    A venue the job has not reached yet falls back to its prior for this hour, labelled as
    one. An empty panel would be a worse answer than a baseline that says what it is.
    """
    # Slug or UUID, like the venue card. A client that navigated by slug should not have to
    # fetch the card first just to learn the id it needs for the live panel.
    venue_id = await lookup.venue_id_for(ctx.session, ident)
    if venue_id is None:
        raise HTTPException(status_code=404, detail="No such venue.")

    row = (
        await ctx.session.execute(
            text(
                """
                SELECT v.id, v.name, v.capacity_covers,
                       l.occupancy, l.sd, l.confidence, l.band, l.trend,
                       l.wait_p50_min, l.wait_p90_min, l.source_weights, l.updated_at
                  FROM venue v
                  LEFT JOIN live_state l ON l.venue_id = v.id
                 WHERE v.id = :v
                """
            ),
            {"v": venue_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such venue")

    if row["occupancy"] is not None:
        occupancy = float(row["occupancy"])
        sd = float(row["sd"])
        confidence = float(row["confidence"])
        weights = row["source_weights"] or {}
        updated_at = row["updated_at"]
        source = "live" if any(k != "prior" for k in weights) else "prior"
    else:
        prior = (
            await ctx.session.execute(
                text(
                    f"""
                    SELECT p.mean_ratio, p.sigma, p.source
                      FROM occupancy_prior p
                      JOIN venue v ON v.id = p.venue_id
                      JOIN city c ON c.id = v.city_id
                     WHERE p.venue_id = :v AND p.hour_of_week = {HOUR_OF_WEEK_SQL}
                    """
                ),
                {"v": venue_id},
            )
        ).mappings().first()
        if prior is None:
            return {
                "venue_id": str(venue_id),
                "state": "unknown",
                # An honest empty rather than a number with nothing behind it.
                "detail": "No observations and no prior for this hour yet.",
            }
        occupancy = float(prior["mean_ratio"])
        sd = float(prior["sigma"])
        confidence = round(max(0.02, min(0.45, 1.0 - sd / fusion.SIGMA_REF)), 3)
        weights = {"prior": 1.0}
        updated_at = None
        source = "prior" if prior["source"] == "google_popular_times" else "archetype"

    band = queueing.wait_band(occupancy, sd, row["capacity_covers"])
    return {
        "venue_id": str(venue_id),
        "occupancy": round(occupancy, 4),
        "sd": round(sd, 4),
        "confidence": round(confidence, 3),
        "band": queueing.state_band(occupancy),
        "trend_per_hour": round(float(row["trend"] or 0.0), 4),
        "wait_p50_min": round(band["p50"], 1),
        "wait_p90_min": round(band["p90"], 1),
        "wait_lo_min": round(band["lo"], 1),
        "wait_hi_min": round(band["hi"], 1),
        "source": source,
        "is_live": source == "live",
        "updated_at": updated_at,
        # The engine panel. Not diagnostics: this is the product's claim that the number can
        # be checked rather than merely believed.
        "sources": fusion.source_lines(weights),
        "at": dt.datetime.now(dt.UTC),
    }
