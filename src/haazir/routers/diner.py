"""What a diner contributes back. Plan §7.

Three writes, and each one is a different kind of evidence.

A **check-in** is a sensor reading: it enters the same fusion as the staff console, weighted
by the reporter's own reputation and verified against the venue's coordinates. A **hold** is
an intent, which is what makes attribution measurable at all. A **fact** is a correction to
the access data, and it is the only path by which `venue.attributes` ever gets better than
what Google knew.

**A check-in is a count, never an identity.** §14 rule 4. The observation stores the reporter
id so their reputation can weight it and so one account cannot report fifty times, and that
is the whole extent of it. Nothing here records who was where for anybody to read back:
`observation` has no user SELECT policy at all.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..auth import ratelimit
from ..auth.deps import Ctx, CurrentUser
from ..services import lookup, realtime

router = APIRouter(prefix="/v1", tags=["diner"])

# A diner sees the room, not the covers. Same bands as the staff console, wider sigma:
# "it looks busy" from a doorway is a real observation and a vaguer one than a host's count.
BAND_VALUE = {"free": 0.20, "moderate": 0.50, "busy": 0.78, "full": 0.95}
CHECKIN_SIGMA = 0.16

# Metres. Wider than the staff geofence because a diner may check in from the queue outside,
# from the car park, or from a table by the window with poor GPS.
CHECKIN_RADIUS_M = 250

# One venue, one person, one report per this many minutes. Without it, one phone can pin a
# restaurant at "full" all evening.
CHECKIN_COOLDOWN_MIN = 45


class CheckinIn(BaseModel):
    # Slug or UUID, like the card, /live and the stream. A client that routed by slug was
    # sending one here and getting a 422 about UUID formatting, which reads as a malformed
    # request rather than "this endpoint wants a different identifier from the last one".
    venue_id: str
    band: str = Field(pattern="^(free|moderate|busy|full)$")
    party_size: int | None = Field(default=None, ge=1, le=30)
    wait_min: int | None = Field(default=None, ge=0, le=240)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)


@router.post("/checkin", status_code=status.HTTP_201_CREATED)
async def checkin(body: CheckinIn, principal: CurrentUser, ctx: Ctx) -> dict:
    """Report what a room looks like from inside it."""
    venue_id = await lookup.venue_id_for(ctx.session, body.venue_id)
    if venue_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such venue")

    venue = (
        await ctx.session.execute(
            text(
                "SELECT id, name, "
                "  ST_Distance(geom, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography) "
                "  AS distance_m "
                "FROM venue WHERE id = :v"
            ),
            {"v": venue_id, "lat": body.lat or 0.0, "lng": body.lng or 0.0},
        )
    ).mappings().first()
    if venue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such venue")

    try:
        ratelimit.check(
            f"checkin:{principal.subject}:{venue_id}",
            limit=1, window_s=CHECKIN_COOLDOWN_MIN * 60,
        )
    except ratelimit.RateLimited as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="You have already reported on this venue recently.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc

    has_position = body.lat is not None and body.lng is not None
    geo_ok = has_position and float(venue["distance_m"]) <= CHECKIN_RADIUS_M

    # Reputation weights the reading; being at the venue is worth more than the reputation.
    # A report from across the city is kept and weighted to near nothing, exactly as an
    # out-of-geofence staff tap is: visible if anyone games it, harmless if they do.
    reputation = await ctx.session.scalar(
        text("SELECT reputation FROM app_user WHERE id = :u"), {"u": principal.subject}
    )
    trust = float(reputation or 0.5) if geo_ok else 0.03

    await ctx.session.execute(
        text(
            """
            INSERT INTO observation (venue_id, source, observed_at, value, sigma,
                                     reporter_id, reporter_trust, geo_ok, payload)
            VALUES (:v, 'checkin', now(), :value, :sigma, :u, :trust, :geo_ok,
                    CAST(:payload AS jsonb))
            """
        ),
        {
            "v": venue_id, "value": BAND_VALUE[body.band], "sigma": CHECKIN_SIGMA,
            "u": principal.subject, "trust": trust, "geo_ok": geo_ok,
            "payload": json.dumps({"band": body.band, "party_size": body.party_size,
                                   "wait_min": body.wait_min,
                                   "distance_m": round(float(venue["distance_m"]), 1)
                                   if has_position else None}),
        },
    )

    visit_id = await ctx.session.scalar(
        text(
            """
            INSERT INTO visit (user_id, venue_id, party_size, arrived_at, wait_reported_min)
            VALUES (:u, :v, :party, now(), :wait)
         RETURNING id
            """
        ),
        {"u": principal.subject, "v": venue_id,
         "party": body.party_size, "wait": body.wait_min},
    )
    await ctx.session.commit()

    from .staff import _refresh_and_publish

    state = await _refresh_and_publish(venue_id)

    return {
        "visit_id": str(visit_id),
        "venue_id": str(venue_id),
        "band": body.band,
        "counted": geo_ok,
        # Said plainly rather than silently discarded, so somebody reporting from home learns
        # why their report did not move anything.
        "note": None if geo_ok else (
            "Recorded, but it will barely affect the estimate: we could not confirm you were "
            "at the venue."
        ),
        "live": {"occupancy": state.get("occupancy"), "band": state.get("band"),
                 "confidence": state.get("confidence")},
    }


class HoldIn(BaseModel):
    party_size: int = Field(ge=1, le=30)
    eta_min: int = Field(default=20, ge=0, le=180)


@router.post("/venues/{venue_id}/hold", status_code=status.HTTP_201_CREATED)
async def hold(venue_id: uuid.UUID, body: HoldIn, principal: CurrentUser, ctx: Ctx) -> dict:
    """"We are on our way." An intent, and the start of an attribution record.

    This is the only honest way to answer an owner asking what this product is worth to them:
    a referral recorded before the visit, matched to a seating afterwards. Counting a
    recommendation as a referral without the diner ever saying they were going would make the
    number meaningless in exactly the direction that flatters us.
    """
    venue = (
        await ctx.session.execute(
            text("SELECT id, name FROM venue WHERE id = :v"), {"v": venue_id}
        )
    ).mappings().first()
    if venue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such venue")

    attribution_id = await ctx.session.scalar(
        text(
            """
            INSERT INTO attribution (venue_id, user_id, referred_at)
            VALUES (:v, :u, now()) RETURNING id
            """
        ),
        {"v": venue_id, "u": principal.subject},
    )
    await ctx.session.commit()

    realtime.hub.publish(
        venue_id, "hold",
        {"party_size": body.party_size, "eta_min": body.eta_min},
    )

    return {
        "attribution_id": str(attribution_id),
        "venue_id": str(venue_id),
        "venue_name": venue["name"],
        "party_size": body.party_size,
        "eta_min": body.eta_min,
        "expected_at": dt.datetime.now(dt.UTC) + dt.timedelta(minutes=body.eta_min),
        # No table is being reserved and the response says so. A "hold" that a venue never
        # agreed to is a promise this product is not in a position to make.
        "note": "The venue has been told you are coming. This is not a reservation.",
    }


class FactIn(BaseModel):
    fact_key: str = Field(min_length=2, max_length=48)
    value: bool | str = Field(description="true, false, or a short value like 'valet'")


# Facts a diner can see and answer for. Kitchen transparency and enforcement history are not
# on this list: they are not observable from a table, and a crowd vote on them would be noise
# dressed as verification.
VERIFIABLE_FACTS = frozenset({
    "prayer_area", "family_section", "wheelchair_accessible", "high_chairs",
    "generator_backup", "accepts_cards", "parking_lot", "valet_parking",
    "outdoor_seating", "air_conditioned", "wifi", "restroom", "serves_vegetarian",
    "halal_certified", "rooftop", "sea_view", "live_music",
})


@router.post("/venues/{venue_id}/facts", status_code=status.HTTP_201_CREATED)
async def verify_fact(
    venue_id: uuid.UUID, body: FactIn, principal: CurrentUser, ctx: Ctx
) -> dict:
    """Confirm or contradict an access fact.

    This is the only route by which `venue.attributes` becomes better than what Google knew,
    and it is why the four facts the plan singles out — prayer area, family section, high
    chairs, generator backup — can ever stop being null. Each verification raises the fact's
    confidence and its count; the nightly decay pulls both back down for anything nobody
    re-confirms.
    """
    if body.fact_key not in VERIFIABLE_FACTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{body.fact_key!r} is not a fact a diner can verify from a table.",
        )

    exists = await ctx.session.scalar(
        text("SELECT 1 FROM venue WHERE id = :v"), {"v": venue_id}
    )
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such venue")

    await ctx.session.execute(
        text(
            """
            INSERT INTO fact_verification (venue_id, fact_key, value, user_id, weight)
            VALUES (:v, :k, CAST(:val AS jsonb), :u, :w)
            """
        ),
        {
            "v": venue_id, "k": body.fact_key, "val": json.dumps(body.value),
            "u": principal.subject,
            # A new account's report counts, and counts less. Reputation is earned by
            # verified visits, not by signing up.
            "w": float(
                await ctx.session.scalar(
                    text("SELECT reputation FROM app_user WHERE id = :u"),
                    {"u": principal.subject},
                ) or 0.5
            ),
        },
    )
    await ctx.session.commit()

    from ..db import service_session

    async with service_session() as s:
        updated = await _fold_fact_into_attributes(s, venue_id, body.fact_key)

    return {"venue_id": str(venue_id), "fact_key": body.fact_key, "recorded": True,
            "fact": updated}


async def _fold_fact_into_attributes(session, venue_id: uuid.UUID, fact_key: str) -> dict:
    """Recompute one fact from its verifications and write it back in the §3.4 shape.

    The value is the weighted majority, the confidence grows with agreement and shrinks with
    disagreement, and `n` is the number of people who actually answered. A fact five diners
    contradict each other about should end up *less* confident than one nobody has touched,
    which is why disagreement subtracts rather than simply failing to add.
    """
    rows = (
        await session.execute(
            text(
                "SELECT value, weight FROM fact_verification "
                " WHERE venue_id = :v AND fact_key = :k"
            ),
            {"v": venue_id, "k": fact_key},
        )
    ).mappings().all()
    if not rows:
        return {}

    tally: dict[str, float] = {}
    for r in rows:
        tally[json.dumps(r["value"])] = tally.get(json.dumps(r["value"]), 0.0) + float(r["weight"])

    winner_json, winner_weight = max(tally.items(), key=lambda kv: kv[1])
    total = sum(tally.values())
    agreement = winner_weight / total
    n = len(rows)

    # Confidence rises with how many people agreed and how strongly they agreed, capped below
    # certainty: no number of diners makes a fact about a building's ramp a proven thing.
    confidence = round(min(0.96, agreement * (1 - 1 / (1 + n * 0.6))), 3)

    fact = {
        "v": json.loads(winner_json),
        "c": confidence,
        "n": n,
        "at": dt.date.today().isoformat(),
        "src": "diner_verified",
    }
    await session.execute(
        text(
            "UPDATE venue SET attributes = attributes || CAST(:patch AS jsonb) "
            " WHERE id = :v"
        ),
        {"v": venue_id, "patch": json.dumps({fact_key: fact})},
    )
    return fact
