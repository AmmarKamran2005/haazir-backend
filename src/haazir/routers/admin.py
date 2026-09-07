"""Admin endpoints that belong to Phase 2: staff device provisioning.

The ingestion and review-queue endpoints from §7 arrive with their phases. Device issuing is
here because a staff console cannot be tested without it.

The raw device token is returned exactly once, in the response to the request that created it.
Only its hash is stored, so it cannot be shown again and a lost tablet is re-provisioned
rather than recovered.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text

from ..auth import device as device_auth
from ..auth.deps import Ctx, CurrentAdmin
from ..config import settings
from ..schemas.auth import DeviceIssueIn, DeviceIssueOut

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.post(
    "/venues/{venue_id}/devices",
    response_model=DeviceIssueOut,
    status_code=status.HTTP_201_CREATED,
)
async def issue_device(
    venue_id: uuid.UUID, body: DeviceIssueIn, principal: CurrentAdmin, ctx: Ctx
) -> DeviceIssueOut:
    exists = await ctx.session.scalar(
        text("SELECT 1 FROM venue WHERE id = :vid"), {"vid": venue_id}
    )
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such venue")

    token, device_id = await device_auth.issue(
        ctx.session,
        venue_id,
        label=body.label,
        geofence_m=body.geofence_m,
        created_by=principal.subject,
    )
    base = settings.web_base_url.rstrip("/")
    return DeviceIssueOut(
        device_id=device_id,
        venue_id=venue_id,
        token=token,
        setup_url=f"{base}/staff/setup?t={token}",
        geofence_m=body.geofence_m,
        expires_in=settings.jwt_device_ttl,
    )


@router.get("/venues/{venue_id}/devices")
async def list_devices(venue_id: uuid.UUID, principal: CurrentAdmin, ctx: Ctx) -> list[dict]:
    """Metadata only. The token is not stored in a form that could be listed."""
    rows = (
        await ctx.session.execute(
            text(
                """
                SELECT id, label, geofence_m, created_at, expires_at,
                       last_used_at, revoked_at
                  FROM device_token
                 WHERE venue_id = :vid
                 ORDER BY created_at DESC
                """
            ),
            {"vid": venue_id},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_device(device_id: uuid.UUID, principal: CurrentAdmin, ctx: Ctx) -> None:
    if not await device_auth.revoke(ctx.session, device_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no such device, or already revoked"
        )
