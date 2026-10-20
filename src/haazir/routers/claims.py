"""Claims and registrations. Plan §4 B2.

Any signed-in user may ask to own a listed venue or register a new one; an admin decides.
Approval is the only path by which `venue.claimed_by` is set for a real owner, and it runs as
the admin, whom the protected-columns trigger lets through.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text

from ..auth.deps import Ctx, CurrentAdmin, CurrentUser
from ..services.normalise import normalise_phone, slugify

owner_router = APIRouter(prefix="/v1/owner", tags=["claims"])
admin_router = APIRouter(prefix="/v1/admin", tags=["claims"])


class NewVenue(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    area_slug: str = Field(min_length=2, max_length=60)
    address: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=24)
    cuisines: list[str] = Field(default_factory=list, max_length=8)
    lat: float = Field(ge=24.7, le=25.2)  # Karachi
    lng: float = Field(ge=66.8, le=67.4)


class ClaimIn(BaseModel):
    venue_id: uuid.UUID | None = None
    new_venue: NewVenue | None = None
    proof_note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _one_target(self) -> ClaimIn:
        if (self.venue_id is None) == (self.new_venue is None):
            raise ValueError("send either venue_id or new_venue")
        return self


class DecideIn(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=500)


CLAIM_COLUMNS = """
    c.id AS claim_id, c.status, c.venue_id, c.new_venue, c.proof_note, c.note,
    c.created_at, c.decided_at, v.name AS venue_name, v.slug AS venue_slug,
    a.name AS area
"""
CLAIM_JOINS = """
    FROM venue_claim c
    LEFT JOIN venue v ON v.id = c.venue_id
    LEFT JOIN area a ON a.id = v.area_id
"""


def _claim(row) -> dict:
    data = dict(row)
    data["claim_id"] = str(data["claim_id"])
    if data["venue_id"]:
        data["venue_id"] = str(data["venue_id"])
    if isinstance(data.get("new_venue"), str):
        data["new_venue"] = json.loads(data["new_venue"])
    return data


@owner_router.post("/claims", status_code=status.HTTP_201_CREATED)
async def create_claim(body: ClaimIn, principal: CurrentUser, ctx: Ctx) -> dict:
    if body.venue_id is not None:
        venue = (
            await ctx.session.execute(
                text("SELECT claimed_by FROM venue WHERE id = :v"), {"v": body.venue_id}
            )
        ).mappings().first()
        if venue is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such venue")
        if venue["claimed_by"] is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This restaurant is already managed by its owner.",
            )
    pending = None
    if body.venue_id is not None:
        pending = await ctx.session.scalar(
            text("SELECT id FROM venue_claim WHERE user_id = :u AND venue_id = :v "
                 "   AND status = 'pending'"),
            {"u": principal.subject, "v": body.venue_id},
        )
    if pending:
        return {"claim_id": str(pending), "status": "pending"}

    claim_id = await ctx.session.scalar(
        text(
            """
            INSERT INTO venue_claim (user_id, venue_id, new_venue, proof_note)
            VALUES (:u, :v, CAST(:nv AS jsonb), :proof)
         RETURNING id
            """
        ),
        {
            "u": principal.subject, "v": body.venue_id,
            "nv": body.new_venue.model_dump_json() if body.new_venue else None,
            "proof": body.proof_note,
        },
    )
    return {"claim_id": str(claim_id), "status": "pending"}


@owner_router.get("/claims")
async def my_claims(principal: CurrentUser, ctx: Ctx) -> list[dict]:
    rows = (
        await ctx.session.execute(
            text(f"SELECT {CLAIM_COLUMNS} {CLAIM_JOINS} WHERE c.user_id = :u "
                 "ORDER BY c.created_at DESC"),
            {"u": principal.subject},
        )
    ).mappings().all()
    return [_claim(r) for r in rows]


@admin_router.get("/claims")
async def list_claims(
    principal: CurrentAdmin, ctx: Ctx,
    status_: str | None = Query(default="pending", alias="status",
                                pattern="^(pending|approved|rejected|all)$"),
) -> list[dict]:
    rows = (
        await ctx.session.execute(
            text(
                f"""
                SELECT {CLAIM_COLUMNS}, u.email AS user_email, u.display_name AS user_name
                  {CLAIM_JOINS} JOIN app_user u ON u.id = c.user_id
                 WHERE (CAST(:s AS text) = 'all' OR c.status = :s)
                 ORDER BY c.created_at DESC LIMIT 200
                """
            ),
            {"s": status_ or "pending"},
        )
    ).mappings().all()
    return [_claim(r) for r in rows]


async def _create_venue(ctx: Ctx, nv: dict, owner_id: uuid.UUID) -> uuid.UUID:
    area_id = await ctx.session.scalar(
        text(
            "SELECT id FROM area WHERE lower(regexp_replace(name, '[^a-zA-Z0-9]+', '-', 'g')) "
            " = lower(:s) OR name ILIKE :s LIMIT 1"
        ),
        {"s": nv["area_slug"]},
    )
    venue_id = await ctx.session.scalar(
        text(
            """
            INSERT INTO venue (slug, name, city_id, area_id, geom, address_full, phone,
                               venue_type, cuisines, claimed_by, claimed_at, tier, status)
            VALUES (:slug, :name, (SELECT id FROM city WHERE name = 'Karachi'), :area,
                    ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography, :address, :phone,
                    'restaurant', CAST(:cuisines AS text[]), :owner, now(), 'claimed', 'active')
         RETURNING id
            """
        ),
        {
            "slug": slugify(nv["name"], uuid.uuid4().hex[:6]), "name": nv["name"],
            "area": area_id, "lat": nv["lat"], "lng": nv["lng"], "address": nv.get("address"),
            "phone": normalise_phone(nv["phone"]) if nv.get("phone") else None,
            "cuisines": [c.strip().lower() for c in nv.get("cuisines") or []], "owner": owner_id,
        },
    )
    # A new venue starts from its area's typical week, so it has an estimate from the first
    # minute rather than an empty panel.
    await ctx.session.execute(
        text(
            """
            INSERT INTO occupancy_prior (venue_id, hour_of_week, mean_ratio, sigma, source)
            SELECT :v, p.hour_of_week, avg(p.mean_ratio), greatest(avg(p.sigma), 0.2),
                   'archetype'
              FROM occupancy_prior p JOIN venue v ON v.id = p.venue_id
             WHERE CAST(:area AS int) IS NULL OR v.area_id = :area
             GROUP BY p.hour_of_week
            """
        ),
        {"v": venue_id, "area": area_id},
    )
    return venue_id


@admin_router.post("/claims/{claim_id}/decide")
async def decide_claim(
    claim_id: uuid.UUID, body: DecideIn, principal: CurrentAdmin, ctx: Ctx
) -> dict:
    claim = (
        await ctx.session.execute(
            text("SELECT id, user_id, venue_id, new_venue, status FROM venue_claim "
                 "WHERE id = :c FOR UPDATE"),
            {"c": claim_id},
        )
    ).mappings().first()
    if claim is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such claim")
    if claim["status"] != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"already {claim['status']}")

    venue_id = claim["venue_id"]
    if body.approve:
        if venue_id is not None:
            result = await ctx.session.execute(
                text(
                    """
                    UPDATE venue SET claimed_by = :u, claimed_at = now(),
                           tier = CASE WHEN tier = 'seeded' THEN 'claimed'::venue_tier
                                       ELSE tier END
                     WHERE id = :v AND (claimed_by IS NULL OR claimed_by = :u)
                    """
                ),
                {"u": claim["user_id"], "v": venue_id},
            )
            if not result.rowcount:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                    detail="the venue was claimed by someone else meanwhile")
        else:
            nv = claim["new_venue"]
            venue_id = await _create_venue(
                ctx, json.loads(nv) if isinstance(nv, str) else nv, claim["user_id"]
            )
        await ctx.session.execute(
            text("UPDATE app_user SET role = 'owner' WHERE id = :u AND role = 'diner'"),
            {"u": claim["user_id"]},
        )

    created = body.approve and claim["venue_id"] is None
    await ctx.session.execute(
        text(
            "UPDATE venue_claim SET status = :s, decided_by = :by, decided_at = now(), "
            "       note = :note"
            # A registration that became a venue now points at it; the check constraint
            # wants exactly one of the two, so the submitted details give way to the row.
            + (", venue_id = :v, new_venue = NULL" if created else "")
            + " WHERE id = :c"
        ),
        {"s": "approved" if body.approve else "rejected", "by": principal.subject,
         "note": body.note, "c": claim_id, **({"v": venue_id} if created else {})},
    )
    return {"claim_id": str(claim_id), "status": "approved" if body.approve else "rejected",
            "venue_id": str(venue_id) if venue_id else None}
