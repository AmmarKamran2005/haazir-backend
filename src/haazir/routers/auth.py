"""Auth endpoints. Plan §5.

**Why verification is a POST and the GET only redirects.** The plan lists
`GET /v1/auth/verify?token=`. A GET that consumes a single-use token is unsafe in practice:
Outlook Safe Links, Gmail's proxy and most corporate mail scanners fetch every URL in an
email before the recipient sees it, and the first fetch would spend the link. So the email
points at the frontend landing page, the GET here is a redirect that changes nothing, and the
frontend POSTs the token back. A scanner following the link now hits a page, not a login.

**Why the refresh cookie's Path is `/v1/auth`.** It is never needed anywhere else, so it is
never sent anywhere else. A request to `/v1/search` cannot carry it, which removes a whole
class of CSRF against every other endpoint without needing a token to defend them.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from ..auth import google as google_auth
from ..auth import group as group_auth
from ..auth import magic_link, ratelimit, refresh
from ..auth.deps import Ctx, CurrentUser, client_ip
from ..auth.jwt import issue_access
from ..config import settings
from ..schemas.auth import (
    DemoIn,
    GoogleIn,
    GroupExchangeIn,
    GroupTokenOut,
    MeOut,
    RefreshIn,
    RequestCodeIn,
    RequestCodeOut,
    RequestLinkIn,
    RequestLinkOut,
    TokenOut,
    UserOut,
    VerifyCodeIn,
    VerifyIn,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

COOKIE_NAME = "hz_refresh"
COOKIE_PATH = "/v1/auth"


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=settings.jwt_refresh_ttl,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
        path=COOKIE_PATH,
        domain=settings.cookie_domain or None,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=COOKIE_NAME, path=COOKIE_PATH, domain=settings.cookie_domain or None
    )


def _access_ttl(role: str) -> int:
    return settings.jwt_admin_ttl if role == "admin" else settings.jwt_access_ttl


@router.post(
    "/request-link", response_model=RequestLinkOut, status_code=status.HTTP_202_ACCEPTED
)
async def request_link(body: RequestLinkIn, request: Request, ctx: Ctx) -> RequestLinkOut:
    """Always 202. Never says whether the address is registered (§5 rule 8)."""
    try:
        link = await magic_link.request_link(
            ctx.session, str(body.email), client_ip(request), body.purpose
        )
    except ratelimit.RateLimited as exc:
        # The one case that does get a different status. Rate limiting has to be visible or it
        # cannot be respected by a client, and it reveals nothing about the address: the limit
        # applies identically to registered and unregistered ones.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many sign-in links requested. Try again shortly.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    # The one asymmetry between environments, and it is deliberate and one-directional.
    return RequestLinkOut(dev_link=None if settings.is_prod else link)


@router.get("/verify", include_in_schema=False)
async def verify_redirect(token: str = "") -> RedirectResponse:
    """Hands the token to the frontend landing page. Consumes nothing: see the module note on
    mail scanners."""
    base = settings.web_base_url.rstrip("/")
    return RedirectResponse(url=f"{base}/auth/verify?token={token}", status_code=302)


@router.post("/verify", response_model=TokenOut)
async def verify(body: VerifyIn, request: Request, response: Response, ctx: Ctx) -> TokenOut:
    try:
        user_id, email, role = await magic_link.consume(ctx.session, body.token)
    except magic_link.LinkInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This link has expired or has already been used. Request a new one.",
        ) from exc

    token, _ = await refresh.issue(
        ctx.session,
        user_id,
        user_agent=request.headers.get("user-agent"),
        ip=client_ip(request),
    )
    _set_refresh_cookie(response, token)
    return TokenOut(
        access_token=issue_access(user_id, role, email),
        expires_in=_access_ttl(role),
        role=role,
        user_id=user_id,
        email=email,
    )


@router.post("/refresh", response_model=TokenOut)
async def refresh_tokens(
    request: Request, response: Response, ctx: Ctx, body: RefreshIn | None = None
) -> TokenOut:
    """Cookie for the web, unchanged. A body `{refresh_token}` for the app, which gets the
    rotated token back in the body and no cookie."""
    mobile = bool(body and body.refresh_token)
    presented = body.refresh_token if mobile else request.cookies.get(COOKIE_NAME)
    if not presented:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="no session")

    try:
        user_id, role, fresh = await refresh.rotate(
            ctx.session,
            presented,
            user_agent=request.headers.get("user-agent"),
            ip=client_ip(request),
        )
    except refresh.RefreshReused as exc:
        # The family is already revoked. Clear the cookie so the client stops replaying it,
        # and say plainly that every session was ended, because the honest explanation is
        # also the one that tells the user to go and check their account.
        await ctx.session.commit()
        _clear_refresh_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This session was reused and all sessions have been signed out for safety.",
        ) from exc
    except refresh.RefreshInvalid as exc:
        _clear_refresh_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired"
        ) from exc

    user = await _user_out(ctx, user_id)
    if not mobile:
        _set_refresh_cookie(response, fresh)
    return TokenOut(
        access_token=issue_access(user_id, role, user.email),
        expires_in=_access_ttl(role),
        role=role,
        user_id=user_id,
        email=user.email,
        refresh_token=fresh if mobile else None,
        user=user,
    )


# --- mobile sign-in ------------------------------------------------------------


async def _user_out(ctx: Ctx, user_id) -> UserOut:
    row = (
        await ctx.session.execute(
            text("SELECT id, email, display_name, role, avatar_url FROM app_user WHERE id = :u"),
            {"u": user_id},
        )
    ).mappings().one()
    return UserOut(id=row["id"], email=row["email"], name=row["display_name"],
                   role=row["role"], avatar_url=row["avatar_url"])


async def _mobile_session(ctx: Ctx, request: Request, user_id, role: str) -> TokenOut:
    """Access and refresh token in the body, for a client that stores them itself."""
    token, _ = await refresh.issue(
        ctx.session, user_id,
        user_agent=request.headers.get("user-agent"), ip=client_ip(request),
    )
    user = await _user_out(ctx, user_id)
    return TokenOut(
        access_token=issue_access(user_id, role, user.email),
        expires_in=_access_ttl(role),
        role=role,
        user_id=user_id,
        email=user.email,
        refresh_token=token,
        user=user,
    )


@router.post("/request-code", response_model=RequestCodeOut)
async def request_code(body: RequestCodeIn, request: Request, ctx: Ctx) -> RequestCodeOut:
    """The app's sign-in: the same email as the magic link, read for its 6-digit code.

    The same answer for every address, registered or not (§5 rule 8).
    """
    try:
        code = await magic_link.request_code(ctx.session, str(body.email), client_ip(request))
    except ratelimit.RateLimited as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many codes requested. Try again shortly.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    return RequestCodeOut(dev_code=None if settings.is_prod else code)


@router.post("/verify-code", response_model=TokenOut)
async def verify_code(body: VerifyCodeIn, request: Request, ctx: Ctx) -> TokenOut:
    try:
        user_id, _email, role = await magic_link.consume_code(
            ctx.session, str(body.email), body.code
        )
    except magic_link.LinkInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That code is wrong or has expired. Request a new one.",
        ) from exc
    return await _mobile_session(ctx, request, user_id, role)


@router.post("/google", response_model=TokenOut)
async def google_sign_in(body: GoogleIn, request: Request, ctx: Ctx) -> TokenOut:
    try:
        who = await google_auth.verify(body.id_token)
    except google_auth.GoogleInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Google sign-in failed."
        ) from exc
    try:
        user_id, _email, role = await magic_link.upsert_user(
            ctx.session, who.email, name=who.name, avatar_url=who.picture, google_sub=who.sub
        )
    except magic_link.LinkInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This account is not active."
        ) from exc
    return await _mobile_session(ctx, request, user_id, role)


DEMO_USERS = {
    "diner": ("demo-diner@haazir.app", "Demo Diner"),
    "owner": ("demo-owner@haazir.app", "Demo Owner"),
}


@router.post("/demo", response_model=TokenOut)
async def demo_sign_in(body: DemoIn, request: Request, ctx: Ctx) -> TokenOut:
    """One-tap accounts for the stage. A 404 unless `DEMO_LOGIN=1`.

    The demo owner owns the hero venue, claimed here on first use, so the owner screens have
    a real restaurant with a real camera behind them.
    """
    if not settings.demo_login:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if body.role == "admin":
        email, name = settings.demo_admin_email, None
    else:
        email, name = DEMO_USERS[body.role]
    from ..db import service_session

    # One service transaction for both writes: the venue's `claimed_by` must see the user
    # row, and the protected-columns trigger lets only admin or service set it.
    async with service_session() as s:
        user_id = await s.scalar(
            text(
                """
                INSERT INTO app_user (email, email_verified, role, display_name, last_seen_at)
                VALUES (:email, TRUE, CAST(:role AS user_role), :name, now())
                ON CONFLICT (email) DO UPDATE
                   SET role = EXCLUDED.role, last_seen_at = now(),
                       display_name = COALESCE(app_user.display_name, EXCLUDED.display_name)
             RETURNING id
                """
            ),
            {"email": email, "role": body.role, "name": name},
        )
        if body.role == "owner":
            await s.execute(
                text(
                    """
                    UPDATE venue SET claimed_by = :u, claimed_at = COALESCE(claimed_at, now()),
                           tier = CASE WHEN tier = 'seeded' THEN 'claimed'::venue_tier
                                       ELSE tier END
                     WHERE slug = :slug AND (claimed_by IS NULL OR claimed_by = :u)
                    """
                ),
                {"u": user_id, "slug": settings.demo_hero_venue},
            )
    return await _mobile_session(ctx, request, user_id, body.role)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, ctx: Ctx) -> Response:
    presented = request.cookies.get(COOKIE_NAME)
    if presented:
        await refresh.revoke_by_token(ctx.session, presented)
    _clear_refresh_cookie(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=MeOut)
async def me(principal: CurrentUser, ctx: Ctx) -> MeOut:
    row = (
        await ctx.session.execute(
            text(
                "SELECT id, email, display_name, role, home_area_id, palate, reputation "
                "FROM app_user WHERE id = :uid"
            ),
            {"uid": principal.subject},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such user")

    await ctx.session.execute(
        text("UPDATE app_user SET last_seen_at = now() WHERE id = :uid"),
        {"uid": principal.subject},
    )
    return MeOut(**dict(row))


@router.post("/group/exchange", response_model=GroupTokenOut)
async def exchange_invite(body: GroupExchangeIn, ctx: Ctx) -> GroupTokenOut:
    """Invite link to guest token. The guest can write its own slot's constraint and read it
    back. There is no path, for any role, to another member's row."""
    try:
        token, group_id, slot = await group_auth.exchange(ctx.session, body.token)
    except group_auth.InviteInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This link has expired, or its answer has already been submitted. Ask the organiser for a new group link.",
        ) from exc
    return GroupTokenOut(
        access_token=token,
        expires_in=settings.jwt_guest_ttl,
        group_id=group_id,
        slot=slot,
    )


@router.get("/debug/last-link", include_in_schema=False)
async def debug_last_link(ctx: Ctx) -> dict:
    """Dev only. Returns when the most recent link was issued, never the token itself.

    Without a mail provider configured the link is printed to the server log; this endpoint
    exists so a developer can confirm one was actually created without reading the log, and
    it is refused outright in production.
    """
    if settings.is_prod:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    row = (
        await ctx.session.execute(
            text(
                "SELECT email, purpose, created_at, expires_at, consumed_at "
                "FROM magic_link ORDER BY created_at DESC LIMIT 1"
            )
        )
    ).mappings().first()
    if row is None:
        return {"status": "none issued"}
    data = dict(row)
    data["expired"] = data["expires_at"] <= dt.datetime.now(dt.UTC)
    return data
