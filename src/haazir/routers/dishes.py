"""Cross-venue price comparison. Plan §7, "two endpoints worth designing carefully".

*"Your Malai Boti is Rs 1,250, the area median is Rs 980."* This is the screen the plan says
brings an owner back weekly, and it is the reason `dish.family` exists: the lookup is on the
family so "Beef Bihari Boti" and "Bihari Boti" land in the same comparison, and `protein`
narrows it so a beef price is never quietly averaged with a chicken one.

Two rules this endpoint keeps.

**Stale prices are shown, never dropped.** Under this much food inflation a six-month-old
price is misleading, so it travels with its age and a `stale` flag and the UI can grey it out.
Filtering it away silently would make the median look better sourced than it is.

**A thin result says so.** `venue_count` and `note` are returned even when only three venues
priced the dish, because a median of three is a number the caller should be allowed to
distrust.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query
from sqlalchemy import text

from ..auth.deps import Ctx
from ..schemas.venue import PriceComparison, PriceQuote
from ..services.normalise import canonicalise_dish

router = APIRouter(prefix="/v1/dishes", tags=["dishes"])

# Food prices in Karachi move fast enough that a quarter-old quote is a different economy.
STALE_AFTER_DAYS = 90
# Below this, a median is arithmetic rather than evidence, and the response says so.
THIN_SAMPLE = 5


@router.get("/{family}/prices", response_model=PriceComparison)
async def dish_prices(
    family: str,
    ctx: Ctx,
    protein: str | None = Query(default=None, description="beef | chicken | mutton | ..."),
    area_id: int | None = Query(default=None),
    lat: float | None = Query(default=None, ge=-90, le=90),
    lng: float | None = Query(default=None, ge=-180, le=180),
    radius_m: int = Query(default=5000, ge=200, le=40_000),
    limit: int = Query(default=60, ge=1, le=200),
) -> PriceComparison:
    """Prices for one dish family across venues, with the median."""
    # The caller may pass a printed name ("Beef Bihari Boti") or a family ("bihari boti").
    # Running it through the same canonicaliser the ingest used means both work, and a
    # protein in the path narrows the query rather than returning nothing.
    parsed = canonicalise_dish(family)
    key = parsed.family if parsed else family.strip().lower()
    protein = protein or (parsed.protein if parsed else None)

    where = ["d.family = :family", "vd.price_pkr IS NOT NULL", "v.status <> 'hidden'"]
    params: dict = {"family": key, "limit": limit}

    if protein:
        where.append("d.protein = :protein")
        params["protein"] = protein
    if area_id is not None:
        where.append("v.area_id = :area_id")
        params["area_id"] = area_id

    distance = "NULL::float8"
    if lat is not None and lng is not None:
        distance = (
            "ST_Distance(v.geom, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography)"
        )
        where.append(f"{distance} <= :radius")
        params |= {"lat": lat, "lng": lng, "radius": radius_m}

    rows = (
        await ctx.session.execute(
            text(
                f"""
                SELECT v.id AS venue_id, v.slug, v.name AS venue_name, a.name AS area,
                       vd.menu_name, vd.price_pkr, vd.price_unit, vd.price_seen_at,
                       d.protein, {distance} AS distance_m
                  FROM venue_dish vd
                  JOIN dish d ON d.id = vd.dish_id
                  JOIN venue v ON v.id = vd.venue_id
                  LEFT JOIN area a ON a.id = v.area_id
                 WHERE {' AND '.join(where)}
                 ORDER BY vd.price_pkr
                 LIMIT :limit
                """
            ),
            params,
        )
    ).mappings().all()

    now = dt.datetime.now(dt.UTC)
    quotes = []
    for r in rows:
        age = (now - r["price_seen_at"]).days if r["price_seen_at"] else None
        quotes.append(
            PriceQuote(
                venue_id=r["venue_id"],
                slug=r["slug"],
                venue_name=r["venue_name"],
                area=r["area"],
                menu_name=r["menu_name"],
                protein=r["protein"],
                price_pkr=r["price_pkr"],
                price_unit=r["price_unit"],
                price_seen_at=r["price_seen_at"],
                price_age_days=age,
                stale=age is not None and age > STALE_AFTER_DAYS,
                distance_m=r["distance_m"],
            )
        )

    prices = sorted(q.price_pkr for q in quotes)
    median = None
    if prices:
        mid = len(prices) // 2
        median = (
            prices[mid]
            if len(prices) % 2
            else round((prices[mid - 1] + prices[mid]) / 2)
        )

    note = None
    if not quotes:
        note = (
            f"No priced menu carries {key!r} yet. Menu coverage is still thin: prices come "
            f"from delivery listings, and most venues in the dataset have none."
        )
    elif len(quotes) < THIN_SAMPLE:
        note = (
            f"Only {len(quotes)} venues price this dish, so the median is indicative rather "
            f"than representative."
        )

    return PriceComparison(
        family=key,
        protein=protein,
        venue_count=len({q.venue_id for q in quotes}),
        median_pkr=median,
        min_pkr=prices[0] if prices else None,
        max_pkr=prices[-1] if prices else None,
        stale_after_days=STALE_AFTER_DAYS,
        quotes=quotes,
        note=note,
    )


@router.get("/search")
async def search_dishes(
    ctx: Ctx,
    q: str = Query(min_length=2, description="partial dish name"),
    limit: int = Query(default=20, ge=1, le=50),
) -> list[dict]:
    """Typeahead over dish families, ranked by how many venues price each one.

    Trigram similarity rather than a prefix match, because the whole point of the alias work
    is that someone typing "behari" finds "bihari boti".
    """
    # Through the same canonicaliser the ingest used. Without this, "behari" cannot find
    # "bihari boti", because ingestion already rewrote the spelling on the way in and the
    # raw query is looking for a string that is no longer stored anywhere.
    parsed = canonicalise_dish(q)
    needle = parsed.family if parsed else q.strip().lower()

    rows = (
        await ctx.session.execute(
            text(
                """
                SELECT d.family,
                       count(DISTINCT vd.venue_id) AS venue_count,
                       max(similarity(d.family, :q)) AS score
                  FROM dish d
                  JOIN venue_dish vd ON vd.dish_id = d.id AND vd.price_pkr IS NOT NULL
                 -- `similarity(a, b) > n` rather than the `%` operator on purpose. asyncpg
                 -- uses numeric placeholders and does no %-escaping, so a literal `%%`
                 -- reaches Postgres as `%%` and fails; and an explicit threshold does not
                 -- depend on whatever pg_trgm.similarity_threshold happens to be set to.
                 WHERE similarity(d.family, :q) > 0.3 OR d.family ILIKE :like
                 GROUP BY d.family
                 ORDER BY venue_count DESC, score DESC
                 LIMIT :limit
                """
            ),
            {"q": needle, "like": f"%{needle}%", "limit": limit},
        )
    ).mappings().all()
    return [dict(r) for r in rows]
