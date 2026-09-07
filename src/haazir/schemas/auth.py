"""Request and response models for the auth endpoints."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, EmailStr, Field


class RequestLinkIn(BaseModel):
    email: EmailStr
    purpose: str = Field(default="login", pattern="^(login|claim)$")


class RequestLinkOut(BaseModel):
    """Identical for every address, registered or not (§5 rule 8)."""

    status: str = "accepted"
    message: str = "If that email is registered, we've sent a link."
    # Present ONLY outside production, where there is usually no mail provider configured and
    # the alternative is reading the link out of a server log. In production this stays None:
    # returning it would hand anyone who can POST an email address a session for it, which is
    # the entire attack the magic link exists to prevent.
    dev_link: str | None = None


class VerifyIn(BaseModel):
    token: str = Field(min_length=8, max_length=512)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str
    user_id: uuid.UUID
    email: str | None = None


class MeOut(BaseModel):
    id: uuid.UUID
    email: str | None
    display_name: str | None
    role: str
    home_area_id: int | None
    palate: dict
    reputation: float


class GroupExchangeIn(BaseModel):
    token: str = Field(min_length=8, max_length=512)


class GroupTokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str = "guest"
    group_id: uuid.UUID
    slot: int


class DeviceIssueIn(BaseModel):
    label: str | None = Field(default=None, max_length=80)
    geofence_m: int = Field(default=300, ge=50, le=2000)


class DeviceIssueOut(BaseModel):
    device_id: uuid.UUID
    venue_id: uuid.UUID
    token: str = Field(description="Shown once. Only its hash is stored.")
    setup_url: str
    geofence_m: int
    expires_in: int
