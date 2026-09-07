"""Venue read endpoints. Plan §7.

`GET /venues/{id}` accepts a slug as well as a UUID, because the frontend routes on
`/v/[slug]` and a card fetched from a shared link should not need a lookup round trip first.

Every response that carries an occupancy number carries `source` alongside it: `live` when an
observation backs it, `prior` when it is the Google or archetype baseline. §14 rule 1 is that
an estimate never travels without its provenance, and the cheapest way to keep that true is
to make the field non-optional in the schema.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import text

from ..auth.deps import Ctx
from ..estimator.fusion import SIGMA_REF
from ..schemas.venue import AreaRef, DishLine, OccupancySummary, VenueCard, VenueDishes
from ..services.clock import HOUR_OF_WEEK_SQL

router = APIRouter(prefix="/v1/venues", tags=["venues"])

# A baseline is never allowed to present as confidently as a live reading, however
# tightly Google's weeks of history happen to agree with themselves.
PRIOR_CONFIDENCE_CAP = 0.45

VENUE_COLUMNS = """
    v.id, v.slug, v.name, v.name_urdu, v.brand, v.branch_label,
    ST_Y(v.geom::geometry) AS lat, ST_X(v.geom::geometry) AS lng,
    v.address_full, v.phone, v.whatsapp, v.website, v.instagram, v.maps_url,
    v.venue_type, v.cuisines, v.price_level, v.avg_ticket_pkr, v.capacity_covers,
    v.google_rating, v.google_review_count, v.hours, v.hours_ramadan,
    v.attributes, v.photos, v.blurb, v.tier, v.status,
    a.id AS area_id, a.name AS area_name, a.name_urdu AS area_name_urdu,
    t.score AS trust_score, t.components AS trust_components,
    l.occupancy, l.sd, l.confidence, l.band, l.wait_p50_min, l.wait_p90_min,
    l.source_weights, l.updated_at
"""

VENUE_JOINS = """
    FROM venue v
    LEFT JOIN area a ON a.id = v.area_id
    LEFT JOIN trust_score t ON t.venue_id = v.id
    LEFT JOIN live_state l ON l.venue_id = v.id
"""


async def _prior_occupancy(ctx: Ctx, venue_id: uuid.UUID) -> OccupancySummary | None:
    """The baseline for a venue nothing has reported on yet.

    Returned as a genuine estimate with its own confidence and `source='prior'`, not as a
    fake live reading and not as an empty panel. The archetype rows are wider than the Google
    ones and the sigma travels with them, so the interval widens honestly.
    """
    # The hour is the venue's, resolved from its city's timezone inside the query. Deriving
    # it from the server clock would read the wrong row by however far the server is from
    # Karachi, and the number that came back would look perfectly reasonable.
    row = (
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
    if row is None:
        return None

    occupancy = float(row["mean_ratio"])
    sd = float(row["sigma"])
    band = (
        "free" if occupancy < 0.35
        else "moderate" if occupancy < 0.65
        else "busy" if occupancy < 0.88
        else "full"
    )
    return OccupancySummary(
        occupancy=round(occupancy, 4),
        sd=round(sd, 4),
        # The same definition the filter uses, `1 - sd / SIGMA_REF`, then capped: nothing
        # modelled should ever look as certain as something measured. An earlier version used
        # its own formula and pushed both a Google aggregate and a category archetype past
        # the cap, so they came back identically confident and the distinction the response
        # claims to draw was not actually there.
        confidence=round(max(0.02, min(PRIOR_CONFIDENCE_CAP, 1.0 - sd / SIGMA_REF)), 3),
        band=band,
        source="prior" if row["source"] == "google_popular_times" else "archetype",
    )


def _card(row) -> VenueCard:
    area = (
        AreaRef(id=row["area_id"], name=row["area_name"], name_urdu=row["area_name_urdu"])
        if row["area_id"]
        else None
    )
    live = (
        OccupancySummary(
            occupancy=row["occupancy"],
            sd=row["sd"],
            confidence=row["confidence"],
            band=row["band"],
            wait_p50_min=row["wait_p50_min"],
            wait_p90_min=row["wait_p90_min"],
            source_weights=row["source_weights"] or {},
            updated_at=row["updated_at"],
            source="live",
        )
        if row["occupancy"] is not None
        else None
    )
    return VenueCard(
        id=row["id"], slug=row["slug"], name=row["name"], name_urdu=row["name_urdu"],
        brand=row["brand"], branch_label=row["branch_label"], area=area,
        lat=row["lat"], lng=row["lng"], address_full=row["address_full"],
        phone=row["phone"], whatsapp=row["whatsapp"], website=row["website"],
        instagram=row["instagram"], maps_url=row["maps_url"],
        venue_type=row["venue_type"], cuisines=list(row["cuisines"] or []),
        price_level=row["price_level"], avg_ticket_pkr=row["avg_ticket_pkr"],
        capacity_covers=row["capacity_covers"], google_rating=row["google_rating"],
        google_review_count=row["google_review_count"], hours=row["hours"],
        hours_ramadan=row["hours_ramadan"], attributes=row["attributes"] or {},
        photos=row["photos"] or [], blurb=row["blurb"], tier=row["tier"],
        status=row["status"], trust_score=row["trust_score"],
        trust_components=row["trust_components"], live=live,
    )


@router.get("/{ident}", response_model=VenueCard)
async def venue_card(ident: str, ctx: Ctx) -> VenueCard:
    """By UUID or slug. RLS hides `status = 'hidden'` without this endpoint asking."""
    try:
        where, param = "v.id = :ident", uuid.UUID(ident)
    except ValueError:
        where, param = "v.slug = :ident", ident

    row = (
        await ctx.session.execute(
            text(f"SELECT {VENUE_COLUMNS} {VENUE_JOINS} WHERE {where}"), {"ident": param}
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such venue")

    card = _card(row)
    if card.live is None:
        card.live = await _prior_occupancy(ctx, card.id)
    return card


@router.get("/{venue_id}/dishes", response_model=VenueDishes)
async def venue_dishes(
    venue_id: uuid.UUID,
    ctx: Ctx,
    priced_only: bool = Query(default=False),
) -> VenueDishes:
    rows = (
        await ctx.session.execute(
            text(
                """
                SELECT vd.dish_id, d.canonical_name, d.family, d.protein,
                       vd.menu_name, vd.section, vd.description, vd.price_pkr,
                       vd.price_unit, vd.price_seen_at, vd.is_signature,
                       vd.sold_out_until, vd.quality_mean
                  FROM venue_dish vd
                  JOIN dish d ON d.id = vd.dish_id
                 WHERE vd.venue_id = :v
                   AND (NOT :priced_only OR vd.price_pkr IS NOT NULL)
                 ORDER BY vd.is_signature DESC, vd.section NULLS LAST, d.canonical_name
                """
            ),
            {"v": venue_id, "priced_only": priced_only},
        )
    ).mappings().all()

    now = dt.datetime.now(dt.UTC)
    dishes = [
        DishLine(
            dish_id=r["dish_id"],
            name=r["canonical_name"],
            menu_name=r["menu_name"],
            family=r["family"],
            protein=r["protein"],
            section=r["section"],
            description=r["description"],
            price_pkr=r["price_pkr"],
            price_unit=r["price_unit"],
            price_seen_at=r["price_seen_at"],
            price_age_days=(now - r["price_seen_at"]).days if r["price_seen_at"] else None,
            is_signature=r["is_signature"],
            sold_out_until=r["sold_out_until"],
            quality_mean=r["quality_mean"],
        )
        for r in rows
    ]
    return VenueDishes(
        venue_id=venue_id,
        count=len(dishes),
        dishes=dishes,
        unpriced=bool(dishes) and all(d.price_pkr is None for d in dishes),
    )
