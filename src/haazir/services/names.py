"""Which venue, if any, did the diner name?

Search reads a query for an area, a cuisine and a dish. That is right for "biryani in North
Nazimabad" and wrong for "zahid nihari", where the diner means one restaurant and the dish word
in its name made the parser answer "any nihari place".

A word points at a particular venue when few venue names contain it. "nihari" or "biryani"
appear in dozens of names and say nothing about which one; "zahid" or "naseeb" appear in a
handful. So the test is the data itself, not a list of cuisines that would go stale: keep the
words that are rare among venue names, and return the venues whose names contain all of them.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# A word in at most this many venue names is specific enough to mean those venues.
MAX_NAMES_PER_WORD = 12

_FILLER = {
    "the", "and", "for", "near", "best", "with", "from", "open", "now", "tonight", "today",
    "mein", "main", "kuch", "khana", "khaana", "wala", "wali", "wale", "abhi", "hai", "hain",
    "chahiye", "jana", "jaana", "kahan", "kaha", "aur", "log", "tak", "min", "minute",
    "restaurant", "cafe", "food", "place",
}


def _words(query: str) -> list[str]:
    return [w for w in dict.fromkeys(re.findall(r"[a-z]+", query.lower()))
            if len(w) >= 3 and w not in _FILLER]


async def venues_named_in(session: AsyncSession, query: str | None, limit: int = 5) -> list[str]:
    """Venue ids whose names the query names, best match first. Empty when it names none."""
    if not query:
        return []
    words = _words(query)[:6]
    if not words:
        return []

    counts = (
        await session.execute(
            text(
                "SELECT w, count(v.id) AS n FROM unnest(CAST(:words AS text[])) AS w "
                "  LEFT JOIN venue v ON v.status = 'active' AND v.name ILIKE '%' || w || '%' "
                " GROUP BY w"
            ),
            {"words": words},
        )
    ).mappings().all()
    rare = [r["w"] for r in counts if 1 <= r["n"] <= MAX_NAMES_PER_WORD]
    if not rare:
        return []

    params: dict = {"q": query, "limit": limit}
    conditions = []
    for i, w in enumerate(rare):
        params[f"w{i}"] = f"%{w}%"
        conditions.append(f"v.name ILIKE :w{i}")
    rows = await session.execute(
        text(
            "SELECT v.id::text FROM venue v WHERE v.status = 'active' AND "
            + " AND ".join(conditions)
            + " ORDER BY similarity(v.name, :q) DESC, v.google_review_count DESC NULLS LAST"
              " LIMIT :limit"
        ),
        params,
    )
    return [r[0] for r in rows]
