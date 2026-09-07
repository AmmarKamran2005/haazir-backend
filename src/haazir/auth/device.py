"""Staff device tokens and the geofence. Plan §5, §3.8.

Staff auth is a device, not a person. Restaurant staff turnover is high and a login tied to
one employee dies with their employment; a token tied to the counter tablet does not. This is
also one of the two principals no managed auth provider can express, which is why the whole
auth layer is ours.

**The geofence records rather than merely rejects.** A state update from outside the radius
gets a 409 *and* an `observation` row with `geo_ok = false`. Silently dropping it would mean
someone testing how far away they can sit and change their competitor's occupancy leaves no
trace. Recorded, the attempt is data: the estimator ignores it and an operator can see it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from .tokens import DEVICE_PREFIX, hash_token, new_token, tokens_match


class DeviceInvalid(Exception):
    """Unknown, revoked or expired device credential."""


@dataclass(frozen=True, slots=True)
class Device:
    id: uuid.UUID
    venue_id: uuid.UUID
    label: str | None
    geofence_m: int


async def issue(
    session: AsyncSession,
    venue_id: uuid.UUID,
    *,
    label: str | None = None,
    geofence_m: int = 300,
    created_by: uuid.UUID | None = None,
    ttl_seconds: int | None = None,
) -> tuple[str, uuid.UUID]:
    """Mint a device token. The raw value is returned once and never recoverable afterwards."""
    token = new_token(DEVICE_PREFIX)
    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(
        seconds=ttl_seconds or settings.jwt_device_ttl
    )
    row = (
        await session.execute(
            text(
                """
                INSERT INTO device_token
                       (venue_id, token_hash, label, geofence_m, expires_at, created_by)
                VALUES (:vid, :hash, :label, :fence, :exp, :by)
             RETURNING id
                """
            ),
            {
                "vid": venue_id,
                "hash": hash_token(token),
                "label": label,
                "fence": geofence_m,
                "exp": expires_at,
                "by": created_by,
            },
        )
    ).mappings().one()
    return token, row["id"]


async def authenticate(session: AsyncSession, presented: str) -> Device:
    """Resolve a device token to its venue, and mark it used.

    `last_used_at` is what makes "90 days, renews on use" true and what lets a device that
    has gone quiet for a month be spotted and revoked.
    """
    if not presented or not presented.startswith(DEVICE_PREFIX):
        raise DeviceInvalid("malformed")

    row = (
        await session.execute(
            text(
                """
                UPDATE device_token
                   SET last_used_at = now()
                 WHERE token_hash = :hash
                   AND revoked_at IS NULL
                   AND expires_at > now()
             RETURNING id, venue_id, label, geofence_m, token_hash
                """
            ),
            {"hash": hash_token(presented)},
        )
    ).mappings().first()

    if row is None:
        raise DeviceInvalid("unknown, revoked or expired")
    if not tokens_match(presented, row["token_hash"]):  # §5 rule 7
        raise DeviceInvalid("mismatch")

    return Device(
        id=row["id"],
        venue_id=row["venue_id"],
        label=row["label"],
        geofence_m=row["geofence_m"],
    )


async def revoke(session: AsyncSession, device_id: uuid.UUID) -> bool:
    result = await session.execute(
        text("UPDATE device_token SET revoked_at = now() WHERE id = :id AND revoked_at IS NULL"),
        {"id": device_id},
    )
    return bool(result.rowcount)


async def distance_to_venue(
    session: AsyncSession, venue_id: uuid.UUID, lat: float, lng: float
) -> float | None:
    """Metres between a reported position and the venue, computed by PostGIS on the geography
    type so it is a real great-circle distance rather than degrees pretending to be metres."""
    return await session.scalar(
        text(
            """
            SELECT ST_Distance(geom, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography)
              FROM venue WHERE id = :vid
            """
        ),
        {"vid": venue_id, "lat": lat, "lng": lng},
    )


async def within_geofence(
    session: AsyncSession, device: Device, lat: float | None, lng: float | None
) -> tuple[bool, float | None]:
    """`(ok, distance_m)`. A device that sends no position fails the check but is not an
    error: the observation is still recorded with `geo_ok = false` and down-weighted."""
    if lat is None or lng is None:
        return False, None
    distance = await distance_to_venue(session, device.venue_id, lat, lng)
    if distance is None:
        return False, None
    return distance <= device.geofence_m, distance
