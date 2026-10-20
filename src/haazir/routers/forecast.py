"""`GET /venues/{ident}/forecast`. The arithmetic is in `estimator/forecast.py`."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import Ctx
from ..estimator import forecast
from ..services import lookup

router = APIRouter(prefix="/v1/venues", tags=["forecast"])


async def forecast_for(session: AsyncSession, venue_id: uuid.UUID, hours: int = 3) -> dict:
    """The week's pattern and the room right now, projected. Shared with the chat agent."""
    prior = {
        r[0]: float(r[1])
        for r in (
            await session.execute(
                text("SELECT hour_of_week, mean_ratio FROM occupancy_prior WHERE venue_id = :v"),
                {"v": venue_id},
            )
        ).all()
    }
    row = (
        await session.execute(
            text(
                "SELECT v.capacity_covers, l.occupancy, l.source_weights "
                "  FROM venue v LEFT JOIN live_state l ON l.venue_id = v.id WHERE v.id = :v"
            ),
            {"v": venue_id},
        )
    ).mappings().first()
    weights = (row["source_weights"] or {}) if row else {}
    # A live_state written from the prior alone is not a deviation from the prior, so only a
    # reading backed by something else pulls the near term away from the weekly pattern.
    measured = any(k != "prior" for k in weights)
    live_now = float(row["occupancy"]) if row and row["occupancy"] is not None and measured else None
    return forecast.project(prior, live_now, hours, row["capacity_covers"] if row else None)


@router.get("/{ident}/forecast")
async def venue_forecast(ident: str, ctx: Ctx, hours: int = Query(default=3, ge=1, le=12)) -> dict:
    venue_id = await lookup.venue_id_for(ctx.session, ident)
    if venue_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such venue")
    return await forecast_for(ctx.session, venue_id, hours)
