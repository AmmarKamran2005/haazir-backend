"""Cameras: registry, zones, ingest, snapshot, and the public table map. Plan §4 B4.

**The ingest is the staff tap at five-second resolution.** A camera box posts per-table
occupancy; the server turns it into one occupancy figure (occupied tables over total), writes
it as an `observation` with `source = 'camera'`, re-fuses, and publishes to every SSE
subscriber on the venue. It is the same path `routers/staff.py` takes, plus a `tables` event
so a phone can redraw the table map without a second request.

**Only counts arrive.** Video never leaves the venue. The optional snapshot is a single frame
the box has already face-blurred, kept so an owner can check the camera is pointed at the
room; it is served only through a short-lived signed URL, never publicly.

**A table map without a camera is labelled for what it is.** `/venues/{ident}/tables` answers
for every venue: from the camera when one is reporting, otherwise laid out from the venue's
zones or capacity and filled from the fused estimate, with `source` saying which.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import random
import time
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text

from ..auth import camera as camera_auth
from ..auth import ratelimit
from ..auth.camera import CurrentCamera
from ..auth.deps import Ctx, CurrentUser
from ..config import settings
from ..db import service_session
from ..services import lookup, realtime
from .staff import _refresh_and_publish

owner_router = APIRouter(prefix="/v1/owner", tags=["camera"])
router = APIRouter(prefix="/v1/camera", tags=["camera"])
public_router = APIRouter(prefix="/v1/venues", tags=["camera"])

# A camera is the sharpest source in the model; the trust is just short of a staff tap inside
# the geofence because a detector can be fooled by a coat on a chair.
CAMERA_SIGMA = 0.05
CAMERA_TRUST = 0.95
# One reading per three seconds per camera. The box sends every five.
INGEST_WINDOW_S = 3
# A reading whose own timestamp is older than this is a replay, and is refused.
STALE_READING_S = 60
# "Online" in the owner's list, and "the table map is the camera's" in the public one.
ONLINE_WITHIN_S = 30
CAMERA_FRESH_S = 120
SNAPSHOT_MAX_BYTES = 1_500_000
SNAPSHOT_URL_TTL_S = 600


# --- shapes ------------------------------------------------------------------


class CameraIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kind: Literal["webcam", "phone", "rtsp"] = "webcam"


class CameraIssued(BaseModel):
    camera_id: uuid.UUID
    token: str = Field(description="Shown once. Only its hash is stored.")


class Zone(BaseModel):
    table_id: str = Field(min_length=1, max_length=40)
    label: str | None = Field(default=None, max_length=40)
    seats: int = Field(default=4, ge=1, le=40)
    polygon: list[list[float]] = Field(min_length=3, max_length=64)

    @model_validator(mode="after")
    def _points(self) -> Zone:
        if any(len(p) != 2 for p in self.polygon):
            raise ValueError("every polygon point is [x, y]")
        return self


class ZonesDoc(BaseModel):
    frame_w: int = Field(ge=1, le=10_000)
    frame_h: int = Field(ge=1, le=10_000)
    zones: list[Zone] = Field(default_factory=list, max_length=80)

    @model_validator(mode="after")
    def _unique_ids(self) -> ZonesDoc:
        ids = [z.table_id for z in self.zones]
        if len(ids) != len(set(ids)):
            raise ValueError("table_id must be unique within a camera")
        return self


class TableReading(BaseModel):
    table_id: str = Field(min_length=1, max_length=40)
    occupied: bool
    persons: int = Field(default=0, ge=0, le=60)


class IngestIn(BaseModel):
    ts: dt.datetime | None = None
    people: int = Field(default=0, ge=0, le=2000)
    tables: list[TableReading] = Field(default_factory=list, max_length=80)
    fps: float | None = Field(default=None, ge=0, le=120)


# --- pure helpers ------------------------------------------------------------


def merge_tables(zones: list[dict], readings: list[dict]) -> list[dict]:
    """The table map: every zone, with its latest reading. Unknown readings are kept too.

    A zone with no reading yet is shown free rather than dropped, so the map does not change
    shape between frames; a reading for a table that is not a zone (the box auto-detected one
    before zones were saved) still appears, with a label made from its id.
    """
    by_id = {r["table_id"]: r for r in readings}
    tables = []
    for z in zones:
        r = by_id.pop(z["table_id"], None)
        tables.append(
            {
                "table_id": z["table_id"],
                "label": z.get("label") or z["table_id"],
                "seats": int(z.get("seats") or 4),
                "occupied": bool(r and r.get("occupied")),
                "persons": int(r.get("persons") or 0) if r else 0,
            }
        )
    for table_id, r in by_id.items():
        tables.append(
            {"table_id": table_id, "label": table_id, "seats": 4,
             "occupied": bool(r.get("occupied")), "persons": int(r.get("persons") or 0)}
        )
    return tables


def occupancy_of(tables: list[dict]) -> float | None:
    """`clamp(occupied / total, 0, 1)`; None when the camera sees no tables at all."""
    if not tables:
        return None
    occupied = sum(1 for t in tables if t["occupied"])
    return max(0.0, min(1.0, occupied / len(tables)))


def estimated_tables(
    venue_id: uuid.UUID, occupancy: float, zones: list[dict], capacity: int | None
) -> list[dict]:
    """A plausible layout filled to the fused occupancy, for venues no camera is watching.

    The order tables fill in is a fixed shuffle seeded by the venue, so the map is stable
    between requests and a rising occupancy adds tables rather than reshuffling them.
    """
    if zones:
        layout = [
            {"table_id": z["table_id"], "label": z.get("label") or z["table_id"],
             "seats": int(z.get("seats") or 4)}
            for z in zones
        ]
    else:
        n = max(4, min(12, round((capacity or 32) / 4)))
        layout = [{"table_id": f"t{i}", "label": f"T{i}", "seats": 4} for i in range(1, n + 1)]
    order = list(range(len(layout)))
    random.Random(venue_id.int).shuffle(order)
    filled = set(order[: round(max(0.0, min(1.0, occupancy)) * len(layout))])
    return [
        {**t, "occupied": i in filled, "persons": round(t["seats"] * 0.75) if i in filled else 0}
        for i, t in enumerate(layout)
    ]


def _sign(camera_id: uuid.UUID, exp: int) -> str:
    msg = f"snapshot:{camera_id}:{exp}".encode()
    return hmac.new(settings.jwt_secret.encode(), msg, hashlib.sha256).hexdigest()[:32]


def snapshot_url(camera_id: uuid.UUID) -> str:
    exp = int(time.time()) + SNAPSHOT_URL_TTL_S
    return f"/v1/camera/{camera_id}/snapshot.jpg?exp={exp}&sig={_sign(camera_id, exp)}"


def _zones(raw) -> list[dict]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    return list(raw or [])


# --- owner: the registry -----------------------------------------------------


async def _manageable(ctx: Ctx, venue_id: uuid.UUID, principal) -> None:
    """The venue's owner, or an admin. 404 otherwise, as in `routers/owner.py`."""
    claimed_by = await ctx.session.scalar(
        text("SELECT claimed_by FROM venue WHERE id = :v"), {"v": venue_id}
    )
    if principal.role == "admin":
        exists = await ctx.session.scalar(
            text("SELECT 1 FROM venue WHERE id = :v"), {"v": venue_id}
        )
        if exists:
            return
    elif claimed_by is not None and claimed_by == principal.subject:
        return
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such venue")


@owner_router.post(
    "/venues/{venue_id}/cameras", response_model=CameraIssued,
    status_code=status.HTTP_201_CREATED,
)
async def add_camera(
    venue_id: uuid.UUID, body: CameraIn, principal: CurrentUser, ctx: Ctx
) -> CameraIssued:
    await _manageable(ctx, venue_id, principal)
    token, camera_id = await camera_auth.issue(
        ctx.session, venue_id, name=body.name, kind=body.kind, created_by=principal.subject
    )
    return CameraIssued(camera_id=camera_id, token=token)


@owner_router.get("/venues/{venue_id}/cameras")
async def list_cameras(venue_id: uuid.UUID, principal: CurrentUser, ctx: Ctx) -> list[dict]:
    await _manageable(ctx, venue_id, principal)
    rows = (
        await ctx.session.execute(
            text(
                """
                SELECT id, name, kind, last_seen, tables_total, tables_occupied, people, fps,
                       snapshot_at, created_at
                  FROM camera
                 WHERE venue_id = :v AND revoked_at IS NULL
                 ORDER BY created_at
                """
            ),
            {"v": venue_id},
        )
    ).mappings().all()
    now = dt.datetime.now(dt.UTC)
    return [
        {
            "camera_id": str(r["id"]),
            "name": r["name"],
            "kind": r["kind"],
            "status": (
                "online"
                if r["last_seen"] and (now - r["last_seen"]).total_seconds() <= ONLINE_WITHIN_S
                else "offline"
            ),
            "last_seen": r["last_seen"],
            "tables_total": r["tables_total"] or 0,
            "tables_occupied": r["tables_occupied"] or 0,
            "people": r["people"] or 0,
            "fps": r["fps"],
            "snapshot_url": snapshot_url(r["id"]) if r["snapshot_at"] else None,
            "snapshot_at": r["snapshot_at"],
        }
        for r in rows
    ]


@owner_router.delete(
    "/venues/{venue_id}/cameras/{camera_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_camera(
    venue_id: uuid.UUID, camera_id: uuid.UUID, principal: CurrentUser, ctx: Ctx
) -> Response:
    await _manageable(ctx, venue_id, principal)
    result = await ctx.session.execute(
        text(
            "UPDATE camera SET revoked_at = now() "
            " WHERE id = :c AND venue_id = :v AND revoked_at IS NULL"
        ),
        {"c": camera_id, "v": venue_id},
    )
    if not result.rowcount:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such camera")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- the camera box ----------------------------------------------------------


@router.get("/zones", response_model=ZonesDoc)
async def get_zones(cam: CurrentCamera) -> ZonesDoc:
    async with service_session() as s:
        row = (
            await s.execute(
                text("SELECT frame_w, frame_h, zones FROM camera WHERE id = :c"), {"c": cam.id}
            )
        ).mappings().one()
    return ZonesDoc(
        frame_w=row["frame_w"] or 640, frame_h=row["frame_h"] or 480, zones=_zones(row["zones"])
    )


@router.put("/zones", response_model=ZonesDoc)
async def put_zones(body: ZonesDoc, cam: CurrentCamera) -> ZonesDoc:
    zones = [z.model_dump() for z in body.zones]
    async with service_session() as s:
        await s.execute(
            text(
                "UPDATE camera SET frame_w = :w, frame_h = :h, zones = CAST(:z AS jsonb), "
                "       tables_total = :n WHERE id = :c"
            ),
            {"w": body.frame_w, "h": body.frame_h, "z": json.dumps(zones), "n": len(zones),
             "c": cam.id},
        )
    return body


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest(body: IngestIn, cam: CurrentCamera) -> dict:
    # A reading replayed after an outage describes a room that has since changed; written as
    # "now" it would overwrite the fresh state, and written at its own time it is already
    # below the fusion's horizon. Refused before the rate limit, so a replay costs nothing.
    if body.ts is not None:
        ts = body.ts if body.ts.tzinfo else body.ts.replace(tzinfo=dt.UTC)
        if (dt.datetime.now(dt.UTC) - ts).total_seconds() > STALE_READING_S:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="This reading is too old to describe the room now.")
    try:
        ratelimit.check(f"camera:{cam.id}", 1, window_s=INGEST_WINDOW_S)
    except ratelimit.RateLimited as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="One reading every few seconds is enough.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc

    readings = [t.model_dump() for t in body.tables]
    now = dt.datetime.now(dt.UTC)
    # The box's clock is not trusted for the fusion's age arithmetic: a laptop five minutes
    # fast would make every reading look fresh for five minutes after it stopped.
    observed_at = now

    async with service_session() as s:
        zones = _zones(
            await s.scalar(text("SELECT zones FROM camera WHERE id = :c"), {"c": cam.id})
        )
        tables = merge_tables(zones, readings)
        value = occupancy_of(tables)
        occupied = sum(1 for t in tables if t["occupied"])
        await s.execute(
            text(
                """
                UPDATE camera SET last_seen = :at, people = :people, fps = :fps,
                       tables_total = :total, tables_occupied = :occ,
                       latest_tables = CAST(:tables AS jsonb)
                 WHERE id = :c
                """
            ),
            {"at": now, "people": body.people, "fps": body.fps, "total": len(tables),
             "occ": occupied, "tables": json.dumps(readings), "c": cam.id},
        )
        if value is not None:
            await s.execute(
                text(
                    """
                    INSERT INTO observation
                           (venue_id, source, observed_at, value, sigma, reporter_trust,
                            geo_ok, payload)
                    VALUES (:v, 'camera', :at, :value, :sigma, :trust, TRUE,
                            CAST(:payload AS jsonb))
                    """
                ),
                {
                    "v": cam.venue_id, "at": observed_at, "value": value,
                    "sigma": CAMERA_SIGMA, "trust": CAMERA_TRUST,
                    "payload": json.dumps(
                        {"camera_id": str(cam.id), "people": body.people, "fps": body.fps,
                         "tables": readings, "box_ts": body.ts.isoformat() if body.ts else None}
                    ),
                },
            )

    state = await _refresh_and_publish(cam.venue_id) if value is not None else {}
    realtime.hub.publish(
        cam.venue_id,
        "tables",
        {"venue_id": str(cam.venue_id), "camera_id": str(cam.id), "source": "camera",
         "updated_at": now.isoformat(), "tables": tables, "people": body.people},
    )
    return {
        "accepted": True,
        "occupancy": round(value, 4) if value is not None else None,
        "tables_total": len(tables),
        "tables_occupied": occupied,
        "band": state.get("band"),
    }


@router.post("/snapshot", status_code=status.HTTP_204_NO_CONTENT)
async def post_snapshot(cam: CurrentCamera, file: Annotated[UploadFile, File()]) -> Response:
    data = await file.read(SNAPSHOT_MAX_BYTES + 1)
    if len(data) > SNAPSHOT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="snapshot is larger than 1.5 MB")
    if not data.startswith(b"\xff\xd8"):
        raise HTTPException(status_code=415, detail="snapshot must be a JPEG")
    async with service_session() as s:
        await s.execute(
            text("UPDATE camera SET snapshot = :b, snapshot_at = now() WHERE id = :c"),
            {"b": data, "c": cam.id},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{camera_id}/snapshot.jpg", include_in_schema=False)
async def get_snapshot(
    camera_id: uuid.UUID, exp: int = Query(...), sig: str = Query(...)
) -> Response:
    if exp < time.time() or not hmac.compare_digest(sig, _sign(camera_id, exp)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="link expired")
    async with service_session() as s:
        data = await s.scalar(
            text("SELECT snapshot FROM camera WHERE id = :c AND revoked_at IS NULL"),
            {"c": camera_id},
        )
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no snapshot")
    return Response(content=bytes(data), media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=5"})


# --- public: the table map ---------------------------------------------------


@public_router.get("/{ident}/tables")
async def venue_tables(ident: str, ctx: Ctx) -> dict:
    venue_id = await lookup.venue_id_for(ctx.session, ident)
    if venue_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such venue")

    cam = (
        await ctx.session.execute(
            text(
                """
                SELECT zones, latest_tables, people, last_seen FROM camera
                 WHERE venue_id = :v AND revoked_at IS NULL
                 ORDER BY last_seen DESC NULLS LAST, created_at
                 LIMIT 1
                """
            ),
            {"v": venue_id},
        )
    ).mappings().first()
    now = dt.datetime.now(dt.UTC)
    zones = _zones(cam["zones"]) if cam else []

    if cam and cam["last_seen"] and (now - cam["last_seen"]).total_seconds() <= CAMERA_FRESH_S:
        tables = merge_tables(zones, _zones(cam["latest_tables"]))
        if tables:
            return {
                "source": "camera", "updated_at": cam["last_seen"],
                "tables": tables, "people": cam["people"] or 0,
            }

    live = (
        await ctx.session.execute(
            text(
                "SELECT v.capacity_covers, l.occupancy, l.source_weights, l.updated_at "
                "  FROM venue v LEFT JOIN live_state l ON l.venue_id = v.id WHERE v.id = :v"
            ),
            {"v": venue_id},
        )
    ).mappings().one()
    occupancy, updated_at = live["occupancy"], live["updated_at"]
    weights = live["source_weights"] or {}
    if occupancy is None:
        from .venues import _prior_occupancy

        prior = await _prior_occupancy(ctx, venue_id)
        occupancy, updated_at = (prior.occupancy if prior else 0.3), now
    tables = estimated_tables(venue_id, float(occupancy), zones, live["capacity_covers"])
    return {
        "source": "staff" if float(weights.get("staff", 0)) >= 0.2 else "estimate",
        "updated_at": updated_at,
        "tables": tables,
        "people": sum(t["persons"] for t in tables),
    }
