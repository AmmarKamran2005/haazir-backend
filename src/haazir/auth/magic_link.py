"""Magic links: issue, send, consume. Plan §5 rules 3, 4, 8.

**Single use is enforced by the database, not by this code.** `magic_link_live` is a unique
index on `token_hash` limited to rows where `consumed_at IS NULL`, and consumption is an
`UPDATE ... WHERE consumed_at IS NULL RETURNING`. Two requests arriving with the same token
at the same moment therefore have exactly one winner, decided by Postgres. A read-then-write
in Python would have a race here, and the race would be "the link works twice".

**The response never depends on whether the address exists.** Not the status, not the body,
not the timing beyond noise. §5 rule 8 exists because "no account found" turns a login form
into a way to test whether someone has an account here.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from urllib.parse import quote

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..services import mail
from . import ratelimit
from .tokens import MAGIC_PREFIX, hash_token, new_token, tokens_match

log = logging.getLogger("haazir.auth")


class LinkInvalid(Exception):
    """Expired, already used, or never existed. The caller must not be told which."""


async def request_link(
    session: AsyncSession,
    email: str,
    ip: str | None,
    purpose: str = "login",
) -> str:
    """Issue and send, returning the link.

    Raises `ratelimit.RateLimited`; every other outcome is silent. The return value exists so
    a non-production deployment can show the link instead of requiring a mailbox — see the
    guard in the router, which is the only place allowed to decide that.
    """
    email = email.strip().lower()
    await ratelimit.check_magic_link(session, email, ip)

    token = new_token(MAGIC_PREFIX)
    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=settings.magic_link_ttl)

    await session.execute(
        text(
            """
            INSERT INTO magic_link (email, token_hash, purpose, expires_at, request_ip)
            VALUES (:email, :hash, :purpose, :expires_at, CAST(:ip AS inet))
            """
        ),
        {
            "email": email,
            "hash": hash_token(token),
            "purpose": purpose,
            "expires_at": expires_at,
            "ip": ip,
        },
    )
    # Committed before the email goes out. If the send fails the row is still counted against
    # the rate limit, which is the direction that cannot be abused.
    await session.commit()

    link = f"{settings.web_base_url.rstrip('/')}/auth/verify?token={quote(token)}"
    subject, body, html = mail.magic_link_email(link, settings.magic_link_ttl // 60)
    await mail.send(email, subject, body, html)
    return link


async def consume(session: AsyncSession, token: str) -> tuple[uuid.UUID, str, str]:
    """Redeem a link. Returns `(user_id, email, role)`, creating the user on first sign-in.

    Raises `LinkInvalid` for expired, consumed, unknown and malformed tokens alike, so the
    error a caller sees carries no information about which it was.
    """
    if not token or not token.startswith(MAGIC_PREFIX):
        raise LinkInvalid("malformed")

    row = (
        await session.execute(
            text(
                """
                UPDATE magic_link
                   SET consumed_at = now()
                 WHERE token_hash = :hash
                   AND consumed_at IS NULL
                   AND expires_at > now()
             RETURNING email, token_hash, purpose
                """
            ),
            {"hash": hash_token(token)},
        )
    ).mappings().first()

    if row is None:
        raise LinkInvalid("expired, already used, or unknown")
    if not tokens_match(token, row["token_hash"]):  # §5 rule 7
        raise LinkInvalid("mismatch")

    email = row["email"]
    admin = email.lower() in settings.admin_email_list

    user = (
        await session.execute(
            text(
                """
                INSERT INTO app_user (email, email_verified, role, last_seen_at)
                VALUES (:email, TRUE, CAST(:role AS user_role), now())
                ON CONFLICT (email) DO UPDATE
                   SET email_verified = TRUE,
                       last_seen_at   = now(),
                       -- An address on the allowlist is promoted, and a role earned inside
                       -- the product (owner) is never demoted back to diner by a login.
                       role = CASE
                                WHEN :is_admin THEN 'admin'::user_role
                                ELSE app_user.role
                              END
             RETURNING id, email, role, status
                """
            ),
            {"email": email, "role": "admin" if admin else "diner", "is_admin": admin},
        )
    ).mappings().one()

    if user["status"] != "active":
        raise LinkInvalid("account is not active")

    return user["id"], user["email"], user["role"]


async def purge_expired(session: AsyncSession) -> int:
    """Nightly. Consumed and expired links have no further purpose and are one more copy of
    an address sitting in a table."""
    result = await session.execute(
        text("DELETE FROM magic_link WHERE expires_at < now() - INTERVAL '1 day'")
    )
    return result.rowcount or 0
