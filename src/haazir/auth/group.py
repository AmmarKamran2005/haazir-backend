"""Group invite tokens. Plan §5, §3.8.

Each member gets a one-time link. Opening it exchanges the opaque invite token for a guest
JWT scoped to exactly one `(group_id, slot)` pair, valid for 24 hours. That JWT authorises
writing that slot's constraint and reading it back, and nothing else anywhere in the product.

The exchange is deliberately one-way. There is no endpoint, for any role, that returns
another member's constraint. The group creator sees who has responded, never what they said.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from .jwt import issue_guest
from .tokens import hash_token, new_token, tokens_match

INVITE_PREFIX = "hzg_"


class InviteInvalid(Exception):
    """Unknown, expired or already-exchanged invite."""


async def issue_invites(
    session: AsyncSession, group_id: uuid.UUID, slots: list[int], ttl_seconds: int | None = None
) -> dict[int, str]:
    """One raw invite token per slot. Returned once; only the hash is kept."""
    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(
        seconds=ttl_seconds or settings.jwt_guest_ttl
    )
    out: dict[int, str] = {}
    for slot in slots:
        token = new_token(INVITE_PREFIX)
        await session.execute(
            text(
                """
                INSERT INTO group_token (group_id, member_slot, token_hash, expires_at)
                VALUES (:gid, :slot, :hash, :exp)
                ON CONFLICT (group_id, member_slot) DO UPDATE
                   SET token_hash  = EXCLUDED.token_hash,
                       expires_at  = EXCLUDED.expires_at,
                       consumed_at = NULL
                """
            ),
            {"gid": group_id, "slot": slot, "hash": hash_token(token), "exp": expires_at},
        )
        out[slot] = token
    return out


async def exchange(session: AsyncSession, presented: str) -> tuple[str, uuid.UUID, int]:
    """Invite token in, guest JWT out. Returns `(jwt, group_id, slot)`.

    Marking the invite consumed is part of the same conditional update that reads it, so two
    people opening the same link at once cannot both get a token for that slot.
    """
    if not presented or not presented.startswith(INVITE_PREFIX):
        raise InviteInvalid("malformed")

    row = (
        await session.execute(
            text(
                """
                UPDATE group_token
                   SET consumed_at = now()
                 WHERE token_hash = :hash
                   AND consumed_at IS NULL
                   AND expires_at > now()
             RETURNING id, group_id, member_slot, token_hash
                """
            ),
            {"hash": hash_token(presented)},
        )
    ).mappings().first()

    if row is None:
        raise InviteInvalid("expired, already used, or unknown")
    if not tokens_match(presented, row["token_hash"]):  # §5 rule 7
        raise InviteInvalid("mismatch")

    # A lookup, not an update. `responded_at` is set when the member submits their
    # constraint, not when they open the link, so that "3 of 6 have responded" means what it
    # says rather than "3 of 6 have opened an email".
    member = (
        await session.execute(
            text("SELECT id FROM group_member WHERE group_id = :gid AND slot = :slot"),
            {"gid": row["group_id"], "slot": row["member_slot"]},
        )
    ).mappings().first()
    if member is None:
        raise InviteInvalid("no such member slot")

    token = issue_guest(row["group_id"], row["member_slot"], member["id"])
    return token, row["group_id"], row["member_slot"]


async def purge_consumed(session: AsyncSession) -> int:
    """Nightly. A consumed invite is spent; keeping the row adds nothing."""
    result = await session.execute(
        text(
            "DELETE FROM group_token "
            "WHERE expires_at < now() - INTERVAL '1 day' "
            "   OR consumed_at < now() - INTERVAL '7 days'"
        )
    )
    return result.rowcount or 0
