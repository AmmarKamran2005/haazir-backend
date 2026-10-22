"""Owner-edited menus and offers, and the price comparison diners use. Plan §4, finale fixes.

**An owner's menu line is a menu line like any other.** It goes through the same dish
canonicaliser as the scraped menus, into the same `dish` and `venue_dish` rows, so search,
the venue page, the price comparison and the chat find "Lotus cake 1 pound" the moment it is
saved. Two lines that canonicalise to the same dish at one venue ("Lotus cake 1 pound" and
"Lotus cake half pound") become two dish rows of one family, so neither overwrites the other
and the comparison still groups them.

**Compare takes the diner's choice of place, not ours.** Either the venues they picked or a
point and a radius; no area is forced on them.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import Ctx, CurrentUser
from ..services import lookup, portion
from ..services.normalise import canonicalise_dish, slugify

owner_router = APIRouter(prefix="/v1/owner", tags=["menu"])
public_router = APIRouter(prefix="/v1/dishes", tags=["menu"])


# --- reading a menu ------------------------------------------------------------


async def menu_for(session: AsyncSession, venue_id: uuid.UUID, *, limit: int = 200,
                   priced_only: bool = False) -> list[dict]:
    """A venue's menu with portions, signature and owner lines first. Shared with the chat."""
    rows = (
        await session.execute(
            text(
                """
                SELECT vd.dish_id, d.canonical_name, d.family, d.protein, vd.menu_name,
                       vd.section, vd.description, vd.price_pkr, vd.price_unit, vd.portion,
                       vd.price_seen_at, vd.is_signature, vd.sold_out_until, vd.owner_added
                  FROM venue_dish vd JOIN dish d ON d.id = vd.dish_id
                 WHERE vd.venue_id = :v AND (NOT :priced OR vd.price_pkr IS NOT NULL)
                 ORDER BY vd.is_signature DESC, vd.owner_added DESC, vd.section NULLS LAST,
                          vd.menu_name
                 LIMIT :limit
                """
            ),
            {"v": venue_id, "priced": priced_only, "limit": limit},
        )
    ).mappings().all()
    now = dt.datetime.now(dt.UTC)
    return [
        {
            "dish_id": str(r["dish_id"]),
            "name": r["canonical_name"],
            "menu_name": r["menu_name"],
            "family": r["family"],
            "protein": r["protein"],
            "section": r["section"],
            "description": r["description"],
            "price_pkr": r["price_pkr"],
            "price_unit": r["price_unit"],
            "portion": portion.label(r["portion"], r["price_unit"], r["menu_name"],
                                     r["description"]),
            "sold_out": bool(r["sold_out_until"] and r["sold_out_until"] > now),
            "is_signature": r["is_signature"],
            "owner_added": r["owner_added"],
        }
        for r in rows
    ]


# --- owner: dishes -------------------------------------------------------------


class DishIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    section: str | None = Field(default=None, max_length=60)
    price_pkr: int | None = Field(default=None, ge=1, le=500_000)
    portion: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, max_length=300)


class DishPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=120)
    section: str | None = Field(default=None, max_length=60)
    price_pkr: int | None = Field(default=None, ge=1, le=500_000)
    portion: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, max_length=300)
    sold_out: bool | None = None


async def _owned_venue(ctx: Ctx, venue_id: uuid.UUID, principal) -> None:
    from .camera import _manageable

    await _manageable(ctx, venue_id, principal)


async def _dish_row(session: AsyncSession, venue_id: uuid.UUID, name: str,
                    portion_label: str | None) -> tuple[uuid.UUID, str | None]:
    """The dish row for a new menu line: canonical, or a variant if the venue has it already."""
    parsed = canonicalise_dish(name)
    if parsed is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="That dish name has no recognisable words.")
    norm = parsed.normalized
    taken = await session.scalar(
        text("SELECT 1 FROM venue_dish vd JOIN dish d ON d.id = vd.dish_id "
             " WHERE vd.venue_id = :v AND d.name_normalized = :n"),
        {"v": venue_id, "n": norm},
    )
    if taken:
        # Same dish, another serving or style: its own row in the same family.
        norm = f"{norm} [{slugify(portion_label or name)}]"
    dish_id = await session.scalar(
        text(
            """
            INSERT INTO dish (canonical_name, name_normalized, family, protein)
            VALUES (:canonical, :norm, :family, :protein)
            ON CONFLICT (name_normalized) DO UPDATE SET family = dish.family
         RETURNING id
            """
        ),
        {"canonical": parsed.canonical, "norm": norm, "family": parsed.family,
         "protein": parsed.protein},
    )
    return dish_id, parsed.price_unit


@owner_router.get("/venues/{venue_id}/dishes")
async def owner_menu(venue_id: uuid.UUID, principal: CurrentUser, ctx: Ctx) -> list[dict]:
    await _owned_venue(ctx, venue_id, principal)
    return await menu_for(ctx.session, venue_id)


@owner_router.post("/venues/{venue_id}/dishes", status_code=status.HTTP_201_CREATED)
async def add_dish(venue_id: uuid.UUID, body: DishIn, principal: CurrentUser, ctx: Ctx) -> dict:
    await _owned_venue(ctx, venue_id, principal)
    label = body.portion or portion.from_text(body.name, body.description)
    dish_id, unit = await _dish_row(ctx.session, venue_id, body.name, label)
    await ctx.session.execute(
        text(
            """
            INSERT INTO venue_dish (venue_id, dish_id, menu_name, section, description,
                                    price_pkr, price_unit, price_seen_at, price_source,
                                    portion, owner_added)
            VALUES (:v, :d, :name, :section, :desc, :price, CAST(:unit AS price_unit),
                    CASE WHEN CAST(:price AS int) IS NULL THEN NULL ELSE now() END, 'owner',
                    :portion, TRUE)
            ON CONFLICT (venue_id, dish_id) DO UPDATE SET
                menu_name = EXCLUDED.menu_name, section = EXCLUDED.section,
                description = EXCLUDED.description, price_pkr = EXCLUDED.price_pkr,
                price_seen_at = EXCLUDED.price_seen_at, price_source = 'owner',
                portion = EXCLUDED.portion
            """
        ),
        {"v": venue_id, "d": dish_id, "name": body.name.strip(), "section": body.section,
         "desc": body.description, "price": body.price_pkr, "unit": unit or "per_plate",
         "portion": label},
    )
    menu = await menu_for(ctx.session, venue_id)
    return next(m for m in menu if m["dish_id"] == str(dish_id))


@owner_router.patch("/venues/{venue_id}/dishes/{dish_id}")
async def edit_dish(venue_id: uuid.UUID, dish_id: uuid.UUID, body: DishPatch,
                    principal: CurrentUser, ctx: Ctx) -> dict:
    await _owned_venue(ctx, venue_id, principal)
    changes = body.model_dump(exclude_unset=True)
    sets, params = [], {"v": venue_id, "d": dish_id}
    for field, column in (("name", "menu_name"), ("section", "section"),
                          ("portion", "portion"), ("description", "description")):
        if field in changes:
            sets.append(f"{column} = :{field}")
            params[field] = changes[field]
    if "price_pkr" in changes:
        sets += ["price_pkr = :price", "price_seen_at = now()", "price_source = 'owner'"]
        params["price"] = changes["price_pkr"]
    if "sold_out" in changes:
        from .staff import _end_of_day_karachi

        sets.append("sold_out_until = :until")
        params["until"] = _end_of_day_karachi() if changes["sold_out"] else None
    if not sets:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="nothing to change")
    result = await ctx.session.execute(
        text(f"UPDATE venue_dish SET {', '.join(sets)} WHERE venue_id = :v AND dish_id = :d"),
        params,
    )
    if not result.rowcount:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="this venue does not serve that dish")
    menu = await menu_for(ctx.session, venue_id)
    return next(m for m in menu if m["dish_id"] == str(dish_id))


@owner_router.delete("/venues/{venue_id}/dishes/{dish_id}",
                     status_code=status.HTTP_204_NO_CONTENT)
async def remove_dish(venue_id: uuid.UUID, dish_id: uuid.UUID, principal: CurrentUser,
                      ctx: Ctx) -> Response:
    await _owned_venue(ctx, venue_id, principal)
    result = await ctx.session.execute(
        text("DELETE FROM venue_dish WHERE venue_id = :v AND dish_id = :d"),
        {"v": venue_id, "d": dish_id},
    )
    if not result.rowcount:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="this venue does not serve that dish")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- owner: offers ---------------------------------------------------------------


class OfferIn(BaseModel):
    title: str = Field(min_length=2, max_length=80)
    discount_pct: int = Field(ge=1, le=60)
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6], max_length=7)
    start_hour: int = Field(default=0, ge=0, le=23)
    end_hour: int = Field(default=23, ge=1, le=24)
    valid_until: dt.date | None = None
    active: bool = True


OFFER_COLUMNS = (
    "id, title, discount_pct, days, weekday, window_start, window_end, expires_at, status"
)


def offer_out(r) -> dict:
    days = list(r["days"]) if r["days"] else ([r["weekday"]] if r["weekday"] is not None
                                              else [0, 1, 2, 3, 4, 5, 6])
    end = r["window_end"]
    return {
        "id": str(r["id"]),
        "title": r["title"] or f"{r['discount_pct']}% off",
        "discount_pct": r["discount_pct"],
        "days": days,
        "start_hour": r["window_start"].hour,
        "end_hour": 24 if (end.hour, end.minute) == (23, 59) else end.hour,
        "valid_until": r["expires_at"].date().isoformat() if r["expires_at"] else None,
        "active": r["status"] == "live",
    }


async def live_offers(session: AsyncSession, venue_id: uuid.UUID) -> list[dict]:
    rows = (
        await session.execute(
            text(f"SELECT {OFFER_COLUMNS} FROM offer WHERE venue_id = :v AND status = 'live' "
                 "  AND (expires_at IS NULL OR expires_at > now()) ORDER BY discount_pct DESC"),
            {"v": venue_id},
        )
    ).mappings().all()
    return [offer_out(r) for r in rows]


@owner_router.get("/venues/{venue_id}/offers")
async def list_offers(venue_id: uuid.UUID, principal: CurrentUser, ctx: Ctx) -> list[dict]:
    await _owned_venue(ctx, venue_id, principal)
    rows = (
        await ctx.session.execute(
            text(f"SELECT {OFFER_COLUMNS} FROM offer WHERE venue_id = :v "
                 " AND status <> 'expired' ORDER BY created_at DESC"),
            {"v": venue_id},
        )
    ).mappings().all()
    return [offer_out(r) for r in rows]


@owner_router.post("/venues/{venue_id}/offers", status_code=status.HTTP_201_CREATED)
async def add_offer(venue_id: uuid.UUID, body: OfferIn, principal: CurrentUser,
                    ctx: Ctx) -> dict:
    await _owned_venue(ctx, venue_id, principal)
    if body.end_hour <= body.start_hour:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="end_hour must be after start_hour")
    days = sorted({d for d in body.days if 0 <= d <= 6}) or [0, 1, 2, 3, 4, 5, 6]
    row = (
        await ctx.session.execute(
            text(
                f"""
                INSERT INTO offer (venue_id, title, discount_pct, days, weekday, window_start,
                                   window_end, status, created_by, expires_at)
                VALUES (:v, :title, :pct, CAST(:days AS smallint[]), NULL, :start, :end,
                        CAST(:status AS offer_status), :by, :expires)
             RETURNING {OFFER_COLUMNS}
                """
            ),
            {
                "v": venue_id, "title": body.title.strip(), "pct": body.discount_pct,
                "days": days, "start": dt.time(body.start_hour),
                "end": dt.time(23, 59) if body.end_hour == 24 else dt.time(body.end_hour),
                "status": "live" if body.active else "paused", "by": principal.subject,
                "expires": (dt.datetime.combine(body.valid_until, dt.time(23, 59),
                                                tzinfo=dt.UTC)
                            if body.valid_until else None),
            },
        )
    ).mappings().one()
    return offer_out(row)


@owner_router.delete("/venues/{venue_id}/offers/{offer_id}",
                     status_code=status.HTTP_204_NO_CONTENT)
async def remove_offer(venue_id: uuid.UUID, offer_id: uuid.UUID, principal: CurrentUser,
                       ctx: Ctx) -> Response:
    await _owned_venue(ctx, venue_id, principal)
    result = await ctx.session.execute(
        text("DELETE FROM offer WHERE id = :o AND venue_id = :v"), {"o": offer_id, "v": venue_id}
    )
    if not result.rowcount:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such offer")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- compare -----------------------------------------------------------------------


@public_router.get("/compare")
async def compare(
    ctx: Ctx,
    dish: str = Query(min_length=2, max_length=80),
    venues: str | None = Query(default=None, description="comma-separated ids or slugs"),
    lat: float | None = Query(default=None, ge=-90, le=90),
    lng: float | None = Query(default=None, ge=-180, le=180),
    radius_m: int = Query(default=5000, ge=200, le=40_000),
    limit: int = Query(default=40, ge=1, le=100),
) -> dict:
    """One dish across the venues the diner picked, or around a point they chose."""
    parsed = canonicalise_dish(dish)
    family = parsed.family if parsed else dish.strip().lower()
    protein = parsed.protein if parsed else None
    raw = re.sub(r"\s+", " ", dish.strip().lower())

    where = ["(d.family = :family OR lower(vd.menu_name) LIKE :like)",
             "vd.price_pkr IS NOT NULL", "v.status = 'active'"]
    params: dict = {"family": family, "like": f"%{raw}%", "limit": limit}
    if protein:
        where.append("(d.protein = :protein OR d.protein IS NULL)")
        params["protein"] = protein

    picked: list[str] = []
    if venues:
        for ident in [v.strip() for v in venues.split(",") if v.strip()][:30]:
            vid = await lookup.venue_id_for(ctx.session, ident)
            if vid:
                picked.append(str(vid))
        if not picked:
            return {"dish": family, "protein": protein, "results": [], "count": 0,
                    "median_pkr": None}
        where.append("v.id = ANY(CAST(:picked AS uuid[]))")
        params["picked"] = picked

    distance = "NULL::float8"
    if lat is not None and lng is not None:
        distance = "ST_Distance(v.geom, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography)"
        params |= {"lat": lat, "lng": lng}
        if not picked:
            where.append(f"{distance} <= :radius")
            params["radius"] = radius_m

    rows = (
        await ctx.session.execute(
            text(
                f"""
                SELECT v.id, v.slug, v.name, a.name AS area, vd.menu_name, vd.price_pkr,
                       vd.price_unit, vd.portion, vd.description, vd.price_seen_at,
                       d.protein, {distance} AS distance_m,
                       l.band, l.occupancy, l.wait_p50_min, l.source_weights
                  FROM venue_dish vd
                  JOIN dish d ON d.id = vd.dish_id
                  JOIN venue v ON v.id = vd.venue_id
                  LEFT JOIN area a ON a.id = v.area_id
                  LEFT JOIN live_state l ON l.venue_id = v.id
                 WHERE {' AND '.join(where)}
                 ORDER BY vd.price_pkr
                 LIMIT :limit
                """
            ),
            params,
        )
    ).mappings().all()

    results = []
    for r in rows:
        weights = r["source_weights"] or {}
        results.append({
            "venue": {"id": str(r["id"]), "slug": r["slug"], "name": r["name"],
                      "area": r["area"]},
            "menu_name": r["menu_name"],
            "protein": r["protein"],
            "price_pkr": r["price_pkr"],
            "portion": portion.label(r["portion"], r["price_unit"], r["menu_name"],
                                     r["description"]),
            "distance_m": round(r["distance_m"]) if r["distance_m"] is not None else None,
            "band": r["band"],
            "occupancy": round(float(r["occupancy"]), 3) if r["occupancy"] is not None else None,
            "wait_min": round(float(r["wait_p50_min"])) if r["wait_p50_min"] is not None else None,
            "live": any(k != "prior" for k in weights),
            "price_seen_at": r["price_seen_at"],
        })
    prices = sorted(x["price_pkr"] for x in results)
    median = None
    if prices:
        mid = len(prices) // 2
        median = prices[mid] if len(prices) % 2 else round((prices[mid - 1] + prices[mid]) / 2)
    return {"dish": family, "protein": protein, "results": results, "count": len(results),
            "median_pkr": median, "min_pkr": prices[0] if prices else None,
            "max_pkr": prices[-1] if prices else None}
