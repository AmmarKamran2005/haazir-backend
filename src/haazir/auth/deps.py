"""FastAPI dependencies: resolve the caller, then tell the database who it is.

There is one entry point, `get_ctx`. It opens a session, works out who is calling, and applies
that identity as the transaction's RLS claims before any route code runs. Everything else in
this module is a thin guard on top of it.

The ordering matters and is the reason this is a single dependency rather than two. A device
token has to be looked up in the database to be verified, so a session must exist *before* the
principal is known. That session therefore starts with anonymous claims and is upgraded once
the principal resolves. A route can never see a session whose claims are more privileged than
its caller, because the only place claims are ever written is here.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import ANON, SessionLocal, apply_claims
from . import device as device_auth
from .jwt import AuthError, Principal, decode
from .tokens import DEVICE_PREFIX

UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="authentication required",
    headers={"WWW-Authenticate": "Bearer"},
)


@dataclass(slots=True)
class AuthContext:
    session: AsyncSession
    principal: Principal | None
    request: Request

    @property
    def role(self) -> str:
        return self.principal.role if self.principal else "anon"

    @property
    def user_id(self) -> uuid.UUID | None:
        p = self.principal
        return p.subject if p and p.role in {"diner", "owner", "admin"} else None

    @property
    def venue_id(self) -> uuid.UUID | None:
        return self.principal.venue_id if self.principal else None

    def require(self, *roles: str) -> Principal:
        if self.principal is None:
            raise UNAUTHORIZED
        if roles and self.principal.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"this endpoint is for {' or '.join(roles)}",
            )
        return self.principal


def bearer(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer" or not credential:
        return None
    return credential.strip()


def client_ip(request: Request) -> str | None:
    """Fly.io terminates TLS and forwards the real address in `Fly-Client-IP`. Without this
    every request looks like it came from the proxy and the per-IP rate limit protects
    nothing."""
    for header in ("fly-client-ip", "cf-connecting-ip", "x-real-ip"):
        value = request.headers.get(header)
        if value:
            return value.split(",")[0].strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def _resolve(request: Request, session: AsyncSession) -> Principal | None:
    credential = bearer(request)
    if not credential:
        return None

    # Device tokens are opaque and database-backed; access tokens are signed JWTs. The prefix
    # decides which, so neither has to be tried speculatively against the other's verifier.
    if credential.startswith(DEVICE_PREFIX):
        try:
            dev = await device_auth.authenticate(session, credential)
        except device_auth.DeviceInvalid as exc:
            raise UNAUTHORIZED from exc
        return Principal(
            role="staff", subject=dev.id, token_type="staff", venue_id=dev.venue_id
        )

    try:
        return decode(credential)
    except AuthError as exc:
        raise UNAUTHORIZED from exc


async def get_ctx(request: Request) -> AsyncIterator[AuthContext]:
    if SessionLocal is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database is not configured",
        )

    async with SessionLocal() as session:
        await apply_claims(session, ANON)
        principal = await _resolve(request, session)
        await apply_claims(session, principal.to_claims() if principal else ANON)
        request.state.principal = principal
        ctx = AuthContext(session=session, principal=principal, request=request)
        try:
            yield ctx
            await session.commit()
        except Exception:
            await session.rollback()
            raise


Ctx = Annotated[AuthContext, Depends(get_ctx)]


def require(*roles: str):
    """Route guard. `dependencies=[Depends(require("owner", "admin"))]`."""

    async def _guard(ctx: Ctx) -> Principal:
        return ctx.require(*roles)

    return _guard


async def current_user(ctx: Ctx) -> Principal:
    return ctx.require("diner", "owner", "admin")


async def current_admin(ctx: Ctx) -> Principal:
    return ctx.require("admin")


async def current_staff(ctx: Ctx) -> Principal:
    return ctx.require("staff")


async def current_guest(ctx: Ctx) -> Principal:
    return ctx.require("guest")


CurrentUser = Annotated[Principal, Depends(current_user)]
CurrentAdmin = Annotated[Principal, Depends(current_admin)]
CurrentStaff = Annotated[Principal, Depends(current_staff)]
CurrentGuest = Annotated[Principal, Depends(current_guest)]
