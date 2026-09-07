"""Karachi Tonight: where the city is full right now. Plan §7.

`cityState` in the prototype loops over every venue in JavaScript. At 1,700 venues that is
fine in a browser and wrong on a server, so this is one aggregate query per endpoint.

The number that matters most here is `live_fraction`. Almost every venue in the dataset is
currently backed by an occupancy prior rather than an observation, and a map that renders a
modelled city exactly like a measured one is a lie of omission. Every response says what
share of it is real.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import text

from ..auth.deps import Ctx
from ..schemas.venue import AreaPulse, CityPulse, CityStats
from ..services.clock import hour_of_week, local_now

router = APIRouter(prefix="/v1/city", tags=["city"])


def _band(occupancy: float) -> str:
    if occupancy < 0.35:
        return "free"
    if occupancy < 0.65:
        return "moderate"
    if occupancy < 0.88:
        return "busy"
    return "full"


# COALESCE is the whole design: a venue with a live estimate contributes that, and one
# without contributes its prior for this hour. Neither is dropped, and the count of how many
# came from each is carried back so the caller can be told.
_OCCUPANCY_PER_VENUE = """
    SELECT v.id, v.area_id, v.capacity_covers,
           COALESCE(l.occupancy, p.mean_ratio) AS occ,
           (l.occupancy IS NOT NULL) AS is_live
      FROM venue v
      JOIN city c ON c.id = v.city_id
      LEFT JOIN live_state l ON l.venue_id = v.id
      LEFT JOIN occupancy_prior p
             -- The venue's local hour, from its own city's timezone. Taking it from the
             -- server clock reads the wrong row by the offset and looks entirely normal.
             ON p.venue_id = v.id
            AND p.hour_of_week = (EXTRACT(DOW  FROM now() AT TIME ZONE c.timezone)::int * 24
                                + EXTRACT(HOUR FROM now() AT TIME ZONE c.timezone)::int)
     WHERE v.status = 'active'
       AND c.name = :city
       AND COALESCE(l.occupancy, p.mean_ratio) IS NOT NULL
"""


@router.get("/pulse", response_model=CityPulse)
async def city_pulse(
    ctx: Ctx,
    city: str = Query(default="Karachi"),
    min_venues: int = Query(default=1, ge=1, description="hide thinly covered areas"),
) -> CityPulse:
    now = local_now()
    how = hour_of_week(now)

    rows = (
        await ctx.session.execute(
            text(
                f"""
                WITH per_venue AS ({_OCCUPANCY_PER_VENUE})
                SELECT a.id AS area_id, a.name, a.name_urdu,
                       ST_Y(a.centroid::geometry) AS lat,
                       ST_X(a.centroid::geometry) AS lng,
                       count(*) AS venue_count,
                       avg(pv.occ) AS occupancy_mean,
                       count(*) FILTER (WHERE pv.is_live) AS live_count
                  FROM per_venue pv
                  JOIN area a ON a.id = pv.area_id
                 GROUP BY a.id, a.name, a.name_urdu, a.centroid
                HAVING count(*) >= :min_venues
                 ORDER BY avg(pv.occ) DESC
                """
            ),
            {"city": city, "min_venues": min_venues},
        )
    ).mappings().all()

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no active venues in {city}"
        )

    areas = [
        AreaPulse(
            area_id=r["area_id"],
            name=r["name"],
            name_urdu=r["name_urdu"],
            lat=r["lat"],
            lng=r["lng"],
            venue_count=r["venue_count"],
            occupancy_mean=round(float(r["occupancy_mean"]), 4),
            band=_band(float(r["occupancy_mean"])),
            live_venue_count=r["live_count"],
        )
        for r in rows
    ]
    total = sum(a.venue_count for a in areas)
    live = sum(a.live_venue_count for a in areas)
    return CityPulse(
        city=city,
        at=now,
        hour_of_week=how,
        areas=areas,
        live_fraction=round(live / total, 4) if total else 0.0,
    )


@router.get("/stats", response_model=CityStats)
async def city_stats(ctx: Ctx, city: str = Query(default="Karachi")) -> CityStats:
    now = local_now()

    row = (
        await ctx.session.execute(
            text(
                f"""
                WITH per_venue AS ({_OCCUPANCY_PER_VENUE})
                SELECT count(*) AS venue_count,
                       avg(occ) AS utilisation_now,
                       count(*) FILTER (WHERE is_live) AS live_count,
                       -- Idle seats only counts venues that told us their capacity. Almost
                       -- none do yet, so this is usually null rather than a guess scaled
                       -- from an assumed average.
                       sum((1 - occ) * capacity_covers) FILTER (
                           WHERE capacity_covers IS NOT NULL) AS idle_seats
                  FROM per_venue
                """
            ),
            {"city": city},
        )
    ).mappings().one()

    if not row["venue_count"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no active venues in {city}"
        )

    weekly = await ctx.session.scalar(
        text(
            """
            SELECT avg(p.mean_ratio)
              FROM occupancy_prior p
              JOIN venue v ON v.id = p.venue_id
             WHERE v.status = 'active'
               AND v.city_id = (SELECT id FROM city WHERE name = :city)
            """
        ),
        {"city": city},
    )

    extremes = (
        await ctx.session.execute(
            text(
                f"""
                WITH per_venue AS ({_OCCUPANCY_PER_VENUE})
                SELECT a.name, avg(pv.occ) AS occ
                  FROM per_venue pv JOIN area a ON a.id = pv.area_id
                 GROUP BY a.name HAVING count(*) >= 5
                 ORDER BY occ DESC
                """
            ),
            {"city": city},
        )
    ).mappings().all()

    return CityStats(
        city=city,
        at=now,
        venue_count=row["venue_count"],
        utilisation_now=round(float(row["utilisation_now"]), 4),
        weekly_mean_utilisation=round(float(weekly or 0), 4),
        idle_seats_now=int(row["idle_seats"]) if row["idle_seats"] is not None else None,
        busiest_area=extremes[0]["name"] if extremes else None,
        quietest_area=extremes[-1]["name"] if extremes else None,
        live_fraction=round(row["live_count"] / row["venue_count"], 4),
    )
