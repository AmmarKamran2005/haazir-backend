"""Staff console. Plan §5, §7, §8.

A one-tap state from the venue's own counter, and the small dashboard the venue gets back in
return. That exchange is the business model in miniature: the restaurant gives the highest
quality signal in the system and is paid in footfall statistics it has no other way to see.

**The tap is the demo.** `POST /staff/state` writes an observation, re-fuses, and publishes to
every SSE subscriber on this venue, so a diner's screen in another window moves within a
second and the confidence bar and source weights visibly change with it.

**A rejected update is still recorded.** An out-of-geofence report is committed with
`geo_ok = false` before the 409 is raised. Dropping it would mean someone probing how far
from a restaurant they can sit and still move its occupancy leaves no trace at all.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..auth import ratelimit
from ..auth.deps import Ctx, CurrentStaff
from ..config import settings
from ..estimator import queueing
from ..services import realtime, recompute

router = APIRouter(prefix="/v1/staff", tags=["staff"])

# What a tap on the console means as an occupancy figure. Mid-band, because a human choosing
# between four buttons is giving a range, and the sigma below says how wide that range is.
BAND_VALUE = {"free": 0.20, "moderate": 0.50, "busy": 0.78, "full": 0.95}
# A staff tap is the most trusted single source in the model and still not a measurement.
BAND_SIGMA = 0.09
# A typed wait is a far sharper statement than one of four buttons, so it narrows the sigma.
TYPED_WAIT_SIGMA = 0.06


class StaffStateIn(BaseModel):
    band: str = Field(pattern="^(free|moderate|busy|full)$")
    wait_min: int | None = Field(default=None, ge=0, le=240)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    note: str | None = Field(default=None, max_length=140)


class StaffStateOut(BaseModel):
    accepted: bool
    venue_id: str
    band: str
    geo_ok: bool
    distance_m: float | None = None
    observed_at: dt.datetime
    occupancy: float | None = None
    confidence: float | None = None
    subscribers_notified: int = 0


@router.post("/state", response_model=StaffStateOut)
async def post_state(body: StaffStateIn, principal: CurrentStaff, ctx: Ctx) -> StaffStateOut:
    venue_id = principal.venue_id
    if venue_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="device is not bound to a venue"
        )

    try:
        ratelimit.check(f"staff:{venue_id}", settings.rl_staff_per_venue_hour, window_s=3600)
    except ratelimit.RateLimited as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many updates for this venue in the last hour.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc

    # Geofence radius, distance and capacity in one statement. They were three separate
    # queries; on a path whose acceptance criterion is measured in milliseconds, two avoidable
    # round trips to another continent are most of the budget.
    checks = (
        await ctx.session.execute(
            text(
                """
                SELECT d.geofence_m, v.capacity_covers,
                       -- The casts are load-bearing: a bare `:lat IS NULL` gives asyncpg
                       -- nothing to infer a parameter type from and it raises
                       -- AmbiguousParameterError before the query ever runs.
                       CASE WHEN CAST(:lat AS float8) IS NULL
                              OR CAST(:lng AS float8) IS NULL THEN NULL
                            ELSE ST_Distance(
                                v.geom,
                                ST_SetSRID(ST_MakePoint(CAST(:lng AS float8),
                                                        CAST(:lat AS float8)),
                                           4326)::geography)
                       END AS distance_m
                  FROM device_token d
                  JOIN venue v ON v.id = d.venue_id
                 WHERE d.id = :device
                """
            ),
            {"device": principal.subject, "lat": body.lat, "lng": body.lng},
        )
    ).mappings().first()
    if checks is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="device is not bound to a venue"
        )

    distance = float(checks["distance_m"]) if checks["distance_m"] is not None else None
    geo_ok = distance is not None and distance <= int(checks["geofence_m"] or 300)

    value, sigma = _value_for(body, checks["capacity_covers"])
    observed_at = dt.datetime.now(dt.UTC)

    await ctx.session.execute(
        text(
            """
            INSERT INTO observation
                   (venue_id, source, observed_at, value, sigma,
                    device_id, reporter_trust, geo_ok, payload)
            VALUES (:vid, 'staff', :at, :value, :sigma,
                    :did, :trust, :geo_ok, CAST(:payload AS jsonb))
            """
        ),
        {
            "vid": venue_id, "at": observed_at, "value": value, "sigma": sigma,
            "did": principal.subject,
            # An out-of-fence report is kept but weighted to almost nothing, rather than
            # being trusted or thrown away.
            "trust": 0.9 if geo_ok else 0.05,
            "geo_ok": geo_ok,
            "payload": _payload(body, distance),
        },
    )
    # Commit before any 409 below, so the attempt survives the rejection.
    await ctx.session.commit()

    if not geo_ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This update was sent from outside the restaurant. It has been recorded but "
                "will not change the live estimate."
            ),
            headers={"X-Geofence-Distance-M": f"{distance:.0f}" if distance else "unknown"},
        )

    # Re-fuse and fan out. This is the path the acceptance criterion times: a staff POST
    # reaching a subscribed SSE client in under 500 ms.
    state = await _refresh_and_publish(venue_id)

    return StaffStateOut(
        accepted=True, venue_id=str(venue_id), band=body.band, geo_ok=geo_ok,
        distance_m=distance, observed_at=observed_at,
        occupancy=state.get("occupancy"), confidence=state.get("confidence"),
        subscribers_notified=realtime.hub.subscriber_count(venue_id),
    )


async def _refresh_and_publish(venue_id: uuid.UUID) -> dict:
    """Fuse under a service session, then publish the public projection.

    The service session is needed because `observation` has no user SELECT policy; the
    payload that goes out is `live_state`, which is public, so nothing privileged leaves.
    """
    from ..db import service_session

    async with service_session() as session:
        await recompute.refresh_live_state(session, [venue_id])
        row = (
            await session.execute(
                text(
                    "SELECT occupancy, sd, confidence, band, wait_p50_min, wait_p90_min, "
                    "       trend, source_weights, updated_at "
                    "  FROM live_state WHERE venue_id = :v"
                ),
                {"v": venue_id},
            )
        ).mappings().first()

    if row is None:
        return {}

    payload = {
        "venue_id": str(venue_id),
        "occupancy": round(float(row["occupancy"]), 4),
        "sd": round(float(row["sd"]), 4),
        "confidence": round(float(row["confidence"]), 3),
        "band": row["band"],
        "wait_p50_min": round(float(row["wait_p50_min"]), 1),
        "wait_p90_min": round(float(row["wait_p90_min"]), 1),
        "trend_per_hour": round(float(row["trend"]), 4),
        "sources": row["source_weights"],
        "updated_at": row["updated_at"],
    }
    realtime.hub.publish(venue_id, "live", payload)
    return payload


# --- what the venue gets back ------------------------------------------------


@router.get("/today")
async def staff_today(principal: CurrentStaff, ctx: Ctx) -> dict:
    """The half of the exchange the restaurant is paid in.

    A venue that taps its state four times a day should be able to see something it could not
    see otherwise: how its day actually ran against its own normal week, and how many of
    today's diners this product sent. Without that the console is unpaid data entry, and it
    will stop being used within a fortnight.
    """
    venue_id = principal.venue_id
    if venue_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="no venue")

    venue = (
        await ctx.session.execute(
            text(
                "SELECT v.name, v.capacity_covers, a.name AS area "
                "  FROM venue v LEFT JOIN area a ON a.id = v.area_id WHERE v.id = :v"
            ),
            {"v": venue_id},
        )
    ).mappings().first()
    if venue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such venue")

    from ..db import service_session

    async with service_session() as s:
        hourly = (
            await s.execute(
                text(
                    """
                    SELECT date_trunc('hour', o.observed_at) AS hour,
                           avg(o.value) FILTER (WHERE o.source <> 'prior') AS observed,
                           avg(o.value) FILTER (WHERE o.source = 'prior')  AS expected,
                           count(*) FILTER (WHERE o.source = 'staff')      AS taps
                      FROM observation o
                     WHERE o.venue_id = :v
                       AND o.observed_at > date_trunc('day', now())
                     GROUP BY 1 ORDER BY 1
                    """
                ),
                {"v": venue_id},
            )
        ).mappings().all()

        state = (
            await s.execute(
                text(
                    "SELECT occupancy, band, confidence, wait_p50_min, source_weights, "
                    "       updated_at FROM live_state WHERE venue_id = :v"
                ),
                {"v": venue_id},
            )
        ).mappings().first()

        week = await s.scalar(
            text("SELECT avg(mean_ratio) FROM occupancy_prior WHERE venue_id = :v"),
            {"v": venue_id},
        )

    referred = (
        await ctx.session.execute(
            text(
                "SELECT count(*) AS n, "
                "       count(*) FILTER (WHERE seated_at IS NOT NULL) AS seated "
                "  FROM attribution WHERE venue_id = :v "
                "   AND referred_at > date_trunc('day', now())"
            ),
            {"v": venue_id},
        )
    ).mappings().one()

    capacity = venue["capacity_covers"]
    return {
        "venue": {"id": str(venue_id), "name": venue["name"], "area": venue["area"],
                  "capacity_covers": capacity},
        "now": dict(state) if state else None,
        "weekly_mean_utilisation": round(float(week), 4) if week is not None else None,
        "hours": [
            {
                "hour": h["hour"],
                "observed": round(float(h["observed"]), 4) if h["observed"] else None,
                "expected": round(float(h["expected"]), 4) if h["expected"] else None,
                "staff_taps": h["taps"],
                # Empty seats, only when the venue told us how many it has. Scaling an
                # assumed average would be inventing the number an owner checks first.
                "empty_covers": (
                    round((1 - float(h["observed"])) * capacity)
                    if h["observed"] and capacity else None
                ),
            }
            for h in hourly
        ],
        "guests_sent_today": referred["n"],
        "guests_seated_today": referred["seated"],
        "at": dt.datetime.now(dt.UTC),
    }


class SoldOutIn(BaseModel):
    until: dt.datetime | None = Field(
        default=None, description="Defaults to the end of today, Karachi time."
    )
    back: bool = Field(default=False, description="Cancel a sold-out mark.")


@router.post("/dish/{dish_id}/soldout")
async def mark_sold_out(
    dish_id: uuid.UUID, body: SoldOutIn, principal: CurrentStaff, ctx: Ctx
) -> dict:
    """"Nihari khatam." The single most useful thing a venue can tell a diner.

    Scoped to the device's own venue by the WHERE clause, not by trusting the path: a device
    token authorises one venue, and a dish id from another venue simply matches nothing.
    """
    venue_id = principal.venue_id
    if venue_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="no venue")

    result = await ctx.session.execute(
        text(
            """
            UPDATE venue_dish SET sold_out_until = :until
             WHERE venue_id = :v AND dish_id = :d
         RETURNING menu_name
            """
        ),
        {
            "v": venue_id,
            "d": dish_id,
            "until": None if body.back else (body.until or _end_of_day_karachi()),
        },
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="this venue does not serve that dish"
        )
    await ctx.session.commit()

    realtime.hub.publish(
        venue_id,
        "dish",
        {"dish_id": str(dish_id), "menu_name": row["menu_name"],
         "sold_out": not body.back},
    )
    return {"dish_id": str(dish_id), "menu_name": row["menu_name"],
            "sold_out": not body.back}


# --- helpers -----------------------------------------------------------------


def _end_of_day_karachi() -> dt.datetime:
    from ..services.clock import local_now

    local = local_now()
    return local.replace(hour=23, minute=59, second=0, microsecond=0)


def _value_for(body: StaffStateIn, capacity: int | None) -> tuple[float, float]:
    """A band alone is one of four buckets. A band plus a typed wait is much sharper.

    Inverting the queueing curve turns "35 minutes" back into occupancy units and blends it
    with the band, which is what the prototype's `staffReport` does. The sigma narrows too,
    because the statement genuinely carries more information.
    """
    target = BAND_VALUE[body.band]
    if body.wait_min is None or body.wait_min <= 0:
        return target, BAND_SIGMA

    from_wait = queueing.occupancy_for_wait(float(body.wait_min), capacity)
    blended = max(0.0, min(0.995, 0.45 * target + 0.55 * from_wait))
    return blended, TYPED_WAIT_SIGMA


def _payload(body: StaffStateIn, distance: float | None) -> str:
    return json.dumps(
        {
            "band": body.band,
            "wait_min": body.wait_min,
            "note": body.note,
            "distance_m": round(distance, 1) if distance is not None else None,
        }
    )
