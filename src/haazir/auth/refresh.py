"""Refresh tokens: rotation with reuse detection. Plan §5 rules 6, 7.

Every refresh token belongs to a `family_id` that begins at login and continues through every
rotation. Presenting a token that has already been rotated means two parties hold the same
credential, so the whole family is revoked rather than that one token. The legitimate holder
is logged out. That is the correct trade: an attacker holding a live refresh token is worse
than a user signing in again.

**The rotation is one atomic statement.** Claiming a token is `UPDATE ... WHERE revoked_at IS
NULL RETURNING`, so two requests racing with the same token have exactly one winner and the
loser is indistinguishable from a replay. A read-then-write in Python would let both through
under concurrency, which is precisely the case reuse detection exists to catch.

They are opaque strings, not JWTs, because revocation is the entire feature and a JWT's
selling point is that it verifies without a lookup.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from .tokens import REFRESH_PREFIX, hash_token, new_token, tokens_match

log = logging.getLogger("haazir.auth")


class RefreshInvalid(Exception):
    """Unknown, expired or malformed. A 401 and nothing more."""


class RefreshReused(Exception):
    """A rotated token came back. The family is revoked by the time this is raised."""

    def __init__(self, family_id: uuid.UUID) -> None:
        super().__init__("refresh token reuse detected")
        self.family_id = family_id


async def issue(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    family_id: uuid.UUID | None = None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[str, uuid.UUID]:
    token = new_token(REFRESH_PREFIX)
    family = family_id or uuid.uuid4()
    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=settings.jwt_refresh_ttl)

    row = (
        await session.execute(
            text(
                """
                INSERT INTO refresh_token
                       (user_id, family_id, token_hash, expires_at, user_agent, ip)
                VALUES (:uid, :fid, :hash, :exp, :ua, CAST(:ip AS inet))
             RETURNING id
                """
            ),
            {
                "uid": user_id,
                "fid": family,
                "hash": hash_token(token),
                "exp": expires_at,
                "ua": (user_agent or "")[:400] or None,
                "ip": ip,
            },
        )
    ).mappings().one()
    return token, row["id"]


async def rotate(
    session: AsyncSession,
    presented: str,
    *,
    user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[uuid.UUID, str, str]:
    """Exchange a refresh token for a new one. Returns `(user_id, role, new_token)`."""
    if not presented or not presented.startswith(REFRESH_PREFIX):
        raise RefreshInvalid("malformed")

    token_hash = hash_token(presented)

    claimed = (
        await session.execute(
            text(
                """
                UPDATE refresh_token
                   SET revoked_at = now()
                 WHERE token_hash = :hash
                   AND revoked_at IS NULL
                   AND expires_at > now()
             RETURNING id, user_id, family_id, token_hash
                """
            ),
            {"hash": token_hash},
        )
    ).mappings().first()

    if claimed is None:
        # Either the token never existed, or it was already spent. Only the second is an
        # attack, and only the second revokes a family.
        prior = (
            await session.execute(
                text("SELECT family_id, expires_at FROM refresh_token WHERE token_hash = :hash"),
                {"hash": token_hash},
            )
        ).mappings().first()
        if prior is None:
            raise RefreshInvalid("unknown token")
        if prior["expires_at"] <= dt.datetime.now(dt.UTC):
            raise RefreshInvalid("expired")
        await revoke_family(session, prior["family_id"])
        log.warning("refresh token reuse; revoked family %s", prior["family_id"])
        raise RefreshReused(prior["family_id"])

    if not tokens_match(presented, claimed["token_hash"]):  # §5 rule 7
        raise RefreshInvalid("mismatch")

    user = (
        await session.execute(
            text("SELECT id, role, status FROM app_user WHERE id = :uid"),
            {"uid": claimed["user_id"]},
        )
    ).mappings().first()
    if user is None or user["status"] != "active":
        await revoke_family(session, claimed["family_id"])
        raise RefreshInvalid("account is not active")

    fresh, new_id = await issue(
        session,
        claimed["user_id"],
        family_id=claimed["family_id"],
        user_agent=user_agent,
        ip=ip,
    )
    await session.execute(
        text("UPDATE refresh_token SET replaced_by = :new WHERE id = :old"),
        {"new": new_id, "old": claimed["id"]},
    )
    return claimed["user_id"], user["role"], fresh


async def revoke_family(session: AsyncSession, family_id: uuid.UUID) -> int:
    result = await session.execute(
        text(
            "UPDATE refresh_token SET revoked_at = now() "
            "WHERE family_id = :fid AND revoked_at IS NULL"
        ),
        {"fid": family_id},
    )
    return result.rowcount or 0


async def revoke_by_token(session: AsyncSession, presented: str) -> int:
    """Logout. Revokes the whole family, so signing out on one device does not leave a live
    chain behind on it."""
    if not presented or not presented.startswith(REFRESH_PREFIX):
        return 0
    row = (
        await session.execute(
            text("SELECT family_id FROM refresh_token WHERE token_hash = :hash"),
            {"hash": hash_token(presented)},
        )
    ).mappings().first()
    if row is None:
        return 0
    return await revoke_family(session, row["family_id"])


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.execute(
        text(
            "UPDATE refresh_token SET revoked_at = now() "
            "WHERE user_id = :uid AND revoked_at IS NULL"
        ),
        {"uid": user_id},
    )
    return result.rowcount or 0
