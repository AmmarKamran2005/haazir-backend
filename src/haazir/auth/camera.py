"""Camera tokens. Same rules as a staff device, a different principal.

A camera box authenticates with an opaque `hzc_` token minted by the venue's owner and shown
once. It can write counts, zones and a snapshot for its own venue and nothing else, so it is
deliberately not a `Principal`: no role in the JWT world maps to it and no RLS claim is ever
derived from it. The routes that accept it resolve it here and write through a service
session, scoped by the camera row's own `venue_id`.

`get_ctx` leaves an `hzc_` bearer alone (anonymous claims) so that this dependency, and only
this dependency, decides what the token is worth.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .tokens import CAMERA_PREFIX, hash_token, new_token, tokens_match


class CameraInvalid(Exception):
    """Unknown, revoked or malformed camera credential."""


@dataclass(frozen=True, slots=True)
class Camera:
    id: uuid.UUID
    venue_id: uuid.UUID
    name: str
    kind: str


async def issue(
    session: AsyncSession,
    venue_id: uuid.UUID,
    *,
    name: str,
    kind: str,
    created_by: uuid.UUID | None = None,
) -> tuple[str, uuid.UUID]:
    """Mint a camera token. The raw value is returned once and never recoverable."""
    token = new_token(CAMERA_PREFIX)
    camera_id = await session.scalar(
        text(
            """
            INSERT INTO camera (venue_id, name, kind, token_hash, created_by)
            VALUES (:v, :name, :kind, :hash, :by)
         RETURNING id
            """
        ),
        {"v": venue_id, "name": name, "kind": kind, "hash": hash_token(token),
         "by": created_by},
    )
    return token, camera_id


async def authenticate(session: AsyncSession, presented: str) -> Camera:
    if not presented or not presented.startswith(CAMERA_PREFIX):
        raise CameraInvalid("malformed")
    row = (
        await session.execute(
            text(
                "SELECT id, venue_id, name, kind, token_hash FROM camera "
                " WHERE token_hash = :hash AND revoked_at IS NULL"
            ),
            {"hash": hash_token(presented)},
        )
    ).mappings().first()
    if row is None or not tokens_match(presented, row["token_hash"]):
        raise CameraInvalid("unknown or revoked")
    return Camera(id=row["id"], venue_id=row["venue_id"], name=row["name"], kind=row["kind"])


async def current_camera(request: Request) -> Camera:
    """FastAPI dependency: the camera behind the bearer token, or 401."""
    from ..db import service_session
    from .deps import UNAUTHORIZED, bearer

    presented = bearer(request)
    if not presented:
        raise UNAUTHORIZED
    try:
        async with service_session() as session:
            return await authenticate(session, presented)
    except CameraInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="camera token is unknown or revoked",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


CurrentCamera = Annotated[Camera, Depends(current_camera)]
