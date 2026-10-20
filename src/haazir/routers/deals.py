"""`GET /deals`. Plan §4 B7."""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..auth.deps import Ctx
from ..services import deals

router = APIRouter(prefix="/v1", tags=["deals"])


@router.get("/deals")
async def list_deals(
    ctx: Ctx,
    bank: str | None = Query(default=None, max_length=40),
    area: str | None = Query(default=None, max_length=60),
    venue: str | None = Query(default=None, max_length=120, description="slug or id"),
    limit: int = Query(default=100, ge=1, le=300),
) -> list[dict]:
    return await deals.search(
        ctx.session, bank=bank, area=area, venue=venue, limit=limit,
        include_demo_flag=ctx.role == "admin",
    )
