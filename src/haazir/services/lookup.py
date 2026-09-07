"""Resolving a venue from whatever the caller had to hand.

A venue has two public identifiers: the UUID, which is its identity, and the slug, which is
what appears in a URL. The card endpoint accepted either from the start; the live endpoints
accepted only the UUID, so a client holding a slug could fetch a venue and then not fetch its
live state. That is the kind of inconsistency nobody notices until a page is half wired.

One definition, used by both, so they cannot drift about what counts as a venue.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def venue_id_for(session: AsyncSession, ident: str) -> uuid.UUID | None:
    """The venue's UUID, given its UUID or its slug. None if there is no such venue.

    RLS still applies: a hidden venue is not visible here either, because this reads `venue`
    as the caller rather than through a service session.
    """
    try:
        candidate = uuid.UUID(ident)
    except (ValueError, AttributeError, TypeError):
        return await session.scalar(
            text("SELECT id FROM venue WHERE slug = :s"), {"s": ident}
        )

    return await session.scalar(
        text("SELECT id FROM venue WHERE id = CAST(:v AS uuid)"), {"v": str(candidate)}
    )
