"""The partner dashboard. Plan §7, §12 Phase 7.

Why a restaurant would ever open this twice. `GET /owner/venues/{id}/prices` is the plan's
answer: *"your Malai Boti is Rs 1,250, the area median is Rs 980."* Nothing else here is worth
a weekly visit, and the analytics exist to make that number make sense.

**An owner may edit what they know and nothing else.** The endpoint accepts a short list of
operational fields; the database refuses the rest independently, through the BEFORE UPDATE
trigger in migration 0010 that resets `tier`, `claimed_by`, `google_rating` and the embedding
to their old values for any role but admin. Phase 7's criterion is that an owner cannot change
those "through any endpoint", and an allow-list here could not promise that on its own: it
only covers the endpoints that exist today.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from ..auth.deps import Ctx, CurrentUser
from ..services.clock import HOUR_OF_WEEK_SQL

router = APIRouter(prefix="/v1/owner", tags=["owner"])


async def _owned(ctx: Ctx, venue_id: uuid.UUID, principal) -> dict:
    """Fetch a venue this caller owns, or 404.

    404 rather than 403 for a venue somebody else owns: "this is not yours" and "this does
    not exist" should be indistinguishable, or the endpoint becomes a way to enumerate which
    venues have been claimed.
    """
    row = (
        await ctx.session.execute(
            text(
                "SELECT v.id, v.name, v.slug, v.claimed_by, v.avg_ticket_pkr, "
                "       v.capacity_covers, a.id AS area_id, a.name AS area "
                "  FROM venue v LEFT JOIN area a ON a.id = v.area_id WHERE v.id = :v"
            ),
            {"v": venue_id},
        )
    ).mappings().first()
    if row is None or row["claimed_by"] != principal.subject:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such venue")
    return dict(row)


@router.get("/venues")
async def my_venues(principal: CurrentUser, ctx: Ctx) -> list[dict]:
    rows = (
        await ctx.session.execute(
            text(
                "SELECT v.id, v.name, v.slug, v.tier, a.name AS area, t.score AS trust_score, "
                "       l.occupancy, l.band, l.updated_at "
                "  FROM venue v "
                "  LEFT JOIN area a ON a.id = v.area_id "
                "  LEFT JOIN trust_score t ON t.venue_id = v.id "
                "  LEFT JOIN live_state l ON l.venue_id = v.id "
                " WHERE v.claimed_by = :u ORDER BY v.name"
            ),
            {"u": principal.subject},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


class VenuePatch(BaseModel):
    """Only what an owner genuinely knows better than we do.

    `extra="forbid"` is the load-bearing line. Pydantic's default is to ignore unknown fields,
    so `{"tier": "live", "capacity_covers": 100}` would apply the capacity, drop the tier and
    return 200 — which an owner would reasonably read as the tier having been changed. A
    refused request is the honest answer to a field they may not set.
    """

    model_config = ConfigDict(extra="forbid")

    phone: str | None = Field(default=None, max_length=24)
    whatsapp: str | None = Field(default=None, max_length=24)
    website: str | None = Field(default=None, max_length=300)
    instagram: str | None = Field(default=None, max_length=120)
    capacity_covers: int | None = Field(default=None, ge=4, le=5000)
    avg_ticket_pkr: int | None = Field(default=None, ge=50, le=100_000)
    blurb: str | None = Field(default=None, max_length=400)


@router.patch("/venues/{venue_id}")
async def update_venue(
    venue_id: uuid.UUID, body: VenuePatch, principal: CurrentUser, ctx: Ctx
) -> dict:
    """Update the operational fields. Everything else is refused underneath this handler."""
    await _owned(ctx, venue_id, principal)

    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not changes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="nothing to change")

    from ..services.normalise import normalise_phone

    for key in ("phone", "whatsapp"):
        if key in changes:
            normalised = normalise_phone(changes[key])
            if normalised is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{changes[key]!r} is not a recognisable Pakistani number",
                )
            changes[key] = normalised

    assignments = ", ".join(f"{k} = :{k}" for k in changes)
    await ctx.session.execute(
        text(f"UPDATE venue SET {assignments} WHERE id = :v AND claimed_by = :u"),
        {**changes, "v": venue_id, "u": principal.subject},
    )

    # A venue whose owner has filled in the operational facts is no longer purely scraped.
    await ctx.session.execute(
        text(
            "INSERT INTO venue_source (venue_id, source, scraped_at, confidence) "
            "VALUES (:v, 'owner', now(), 1.0)"
        ),
        {"v": venue_id},
    )
    return {"venue_id": str(venue_id), "updated": sorted(changes)}


@router.get("/venues/{venue_id}/prices")
async def price_position(venue_id: uuid.UUID, principal: CurrentUser, ctx: Ctx) -> dict:
    """★ The screen that brings an owner back weekly.

    Every priced dish on this menu against the median for the same dish family in the same
    area. Comparison is by `family` and narrowed by `protein`, so a beef price is never put
    beside a chicken one and reported as a gap the owner should close.
    """
    venue = await _owned(ctx, venue_id, principal)

    rows = (
        await ctx.session.execute(
            text(
                """
                WITH mine AS (
                    SELECT vd.dish_id, vd.menu_name, vd.price_pkr, vd.price_seen_at,
                           d.family, d.protein
                      FROM venue_dish vd JOIN dish d ON d.id = vd.dish_id
                     WHERE vd.venue_id = :v AND vd.price_pkr IS NOT NULL
                ),
                area_prices AS (
                    SELECT d.family, d.protein, vd.price_pkr
                      FROM venue_dish vd
                      JOIN dish d ON d.id = vd.dish_id
                      JOIN venue v ON v.id = vd.venue_id
                     WHERE vd.price_pkr IS NOT NULL
                       AND v.id <> :v
                       AND v.status = 'active'
                       -- The cast is required. A bare `:area_id IS NULL` leaves asyncpg with
                       -- nothing to infer the parameter's type from and it raises
                       -- AmbiguousParameterError before the query reaches Postgres at all.
                       AND (CAST(:area_id AS int) IS NULL OR v.area_id = :area_id)
                )
                SELECT m.dish_id, m.menu_name, m.family, m.protein, m.price_pkr,
                       m.price_seen_at,
                       count(a.price_pkr)                            AS comparable_venues,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY a.price_pkr)::int
                                                                     AS area_median,
                       min(a.price_pkr)                              AS area_min,
                       max(a.price_pkr)                              AS area_max
                  FROM mine m
                  LEFT JOIN area_prices a
                         ON a.family = m.family
                        AND a.protein IS NOT DISTINCT FROM m.protein
                 GROUP BY m.dish_id, m.menu_name, m.family, m.protein, m.price_pkr,
                          m.price_seen_at
                 ORDER BY m.menu_name
                """
            ),
            {"v": venue_id, "area_id": venue["area_id"]},
        )
    ).mappings().all()

    now = dt.datetime.now(dt.UTC)
    dishes = []
    for r in rows:
        median = r["area_median"]
        dishes.append(
            {
                "dish_id": str(r["dish_id"]),
                "menu_name": r["menu_name"],
                "family": r["family"],
                "protein": r["protein"],
                "your_price_pkr": r["price_pkr"],
                "area_median_pkr": median,
                "area_min_pkr": r["area_min"],
                "area_max_pkr": r["area_max"],
                "comparable_venues": r["comparable_venues"],
                "delta_pkr": (r["price_pkr"] - median) if median else None,
                "delta_pct": (
                    round((r["price_pkr"] - median) / median * 100, 1) if median else None
                ),
                "price_age_days": (now - r["price_seen_at"]).days if r["price_seen_at"] else None,
                # Below this the median is arithmetic, not evidence, and the UI should say so
                # rather than telling an owner to move their price on two data points.
                "comparable": (r["comparable_venues"] or 0) >= 3,
            }
        )

    comparable = [d for d in dishes if d["comparable"]]
    return {
        "venue_id": str(venue_id),
        "venue_name": venue["name"],
        "area": venue["area"],
        "dishes": dishes,
        "priced_dishes": len(dishes),
        "comparable_dishes": len(comparable),
        "note": (
            "Prices come from delivery listings and are compared within your area, matched on "
            "dish family and protein. Where fewer than three other venues price the same dish, "
            "the median is shown but marked as not comparable."
        ),
    }


@router.get("/venues/{venue_id}/analytics")
async def analytics(venue_id: uuid.UUID, principal: CurrentUser, ctx: Ctx) -> dict:
    """The hour-by-day heatmap, and where this venue's quiet windows are.

    The point of the heatmap is not that a restaurant learns Friday is busy; it knows. It is
    the empty windows, because that is where an offer can be aimed at a real gap rather than
    discounting a night that was going to be full anyway.
    """
    venue = await _owned(ctx, venue_id, principal)

    from ..db import service_session

    async with service_session() as s:
        prior = (
            await s.execute(
                text(
                    "SELECT hour_of_week, mean_ratio, source FROM occupancy_prior "
                    " WHERE venue_id = :v ORDER BY hour_of_week"
                ),
                {"v": venue_id},
            )
        ).mappings().all()

        observed = (
            await s.execute(
                text(
                    """
                    SELECT (EXTRACT(DOW FROM o.observed_at AT TIME ZONE 'Asia/Karachi')::int
                            * 24
                            + EXTRACT(HOUR FROM o.observed_at AT TIME ZONE 'Asia/Karachi')::int
                           ) AS hour_of_week,
                           avg(o.value) AS occupancy, count(*) AS n
                      FROM observation o
                     WHERE o.venue_id = :v AND o.source <> 'prior'
                       AND o.observed_at > now() - INTERVAL '28 days'
                     GROUP BY 1
                    """
                ),
                {"v": venue_id},
            )
        ).mappings().all()

        now_row = (
            await s.execute(
                text(
                    f"SELECT {HOUR_OF_WEEK_SQL} AS how FROM city c "
                    " JOIN venue v ON v.city_id = c.id WHERE v.id = :v"
                ),
                {"v": venue_id},
            )
        ).mappings().first()

    observed_by_hour = {r["hour_of_week"]: r for r in observed}
    grid = []
    for r in prior:
        how = r["hour_of_week"]
        seen = observed_by_hour.get(how)
        grid.append(
            {
                "hour_of_week": how,
                "weekday": how // 24,
                "hour": how % 24,
                "expected": round(float(r["mean_ratio"]), 4),
                "observed": round(float(seen["occupancy"]), 4) if seen else None,
                "observations": seen["n"] if seen else 0,
                "source": r["source"],
            }
        )

    # Quiet windows during hours a restaurant is actually open. A 4am trough is not an
    # opportunity, it is the middle of the night.
    trading = [g for g in grid if 11 <= g["hour"] <= 23]
    quietest = sorted(trading, key=lambda g: g["expected"])[:8]

    area_median = await ctx.session.scalar(
        text(
            """
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY x.occ)
              FROM (
                SELECT avg(p.mean_ratio) AS occ
                  FROM occupancy_prior p JOIN venue v ON v.id = p.venue_id
                 WHERE v.area_id = :area AND v.status = 'active'
                 GROUP BY v.id
              ) x
            """
        ),
        {"area": venue["area_id"]},
    )
    mine = sum(g["expected"] for g in grid) / len(grid) if grid else 0.0

    return {
        "venue_id": str(venue_id),
        "venue_name": venue["name"],
        "area": venue["area"],
        "hour_of_week_now": now_row["how"] if now_row else None,
        "grid": grid,
        "weekly_mean": round(mine, 4),
        "area_weekly_median": round(float(area_median), 4) if area_median else None,
        "quiet_windows": [
            {"weekday": g["weekday"], "hour": g["hour"], "expected": g["expected"]}
            for g in quietest
        ],
        # Said plainly: almost none of this is measured yet, and an owner reading a heatmap
        # should know whether they are looking at their restaurant or at a category average.
        "observed_hours": len(observed_by_hour),
        "note": (
            f"{len(observed_by_hour)} of 168 hours have live observations; the rest is this "
            f"venue's occupancy prior, from "
            + ("Google's popular times." if prior and prior[0]["source"] == "google_popular_times"
               else "a category archetype.")
        ),
    }


@router.get("/venues/{venue_id}/attribution")
async def attribution(venue_id: uuid.UUID, principal: CurrentUser, ctx: Ctx) -> dict:
    """How many guests this product sent, and how many of them sat down.

    Referrals and seatings are reported separately and unverified ones are not folded into the
    total. A number that flatters us is worth nothing to the person deciding whether to pay
    for it.
    """
    await _owned(ctx, venue_id, principal)

    row = (
        await ctx.session.execute(
            text(
                """
                SELECT count(*) AS referred,
                       count(*) FILTER (WHERE seated_at IS NOT NULL) AS seated,
                       count(*) FILTER (WHERE receipt_verified) AS verified,
                       COALESCE(sum(amount_pkr) FILTER (WHERE receipt_verified), 0) AS revenue,
                       count(*) FILTER (WHERE referred_at > now() - INTERVAL '7 days')
                           AS referred_7d
                  FROM attribution WHERE venue_id = :v
                """
            ),
            {"v": venue_id},
        )
    ).mappings().one()

    return {
        "venue_id": str(venue_id),
        "referred_total": row["referred"],
        "referred_last_7_days": row["referred_7d"],
        "seated": row["seated"],
        "receipt_verified": row["verified"],
        "verified_revenue_pkr": int(row["revenue"]),
        "note": (
            "A referral is counted when a diner says they are on their way, not when we show "
            "your venue. Seatings and verified receipts are reported separately and never "
            "folded into the referral count."
        ),
    }
