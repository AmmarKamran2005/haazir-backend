"""Pulling the destination and the kind of food out of what somebody typed.

Both were being dropped. "biryani in North Nazimabad" and "chinese" arrived as `text` on the
search request, and `text` was never read: the ranking was distance from a default origin
against no cuisine filter, so the first answer to "chinese" was a bakery in Saddar and the
first answer to "North Nazimabad" was Burns Road. The filters existed — `v.area_id` and
`v.cuisines @> ARRAY[...]` — and nothing ever set them.

Server-side rather than in the client parser because both `/v1/search` and `/v1/ask` need it,
and because the list of areas is a table: matching against the database's own names is the
only way this cannot drift from the data.

Longest match first. "North Nazimabad" and "Nazimabad" are both areas, and a naive scan finds
the shorter one inside the longer one and sends the diner four kilometres away.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Cuisines as the data spells them, plus the words people actually type for them. Only where
# the alias is unambiguous: "asian" is not folded into "chinese", because somebody asking for
# asian food and being handed only Chinese is a worse answer than a broad one.
CUISINE_ALIASES: dict[str, tuple[str, ...]] = {
    "chinese": ("chinese", "chinis", "chineese"),
    "fast food": ("fast food", "fastfood", "burger", "burgers", "fries"),
    "bbq": ("bbq", "barbecue", "barbeque", "tikka", "boti", "seekh"),
    "biryani": ("biryani", "biriyani", "biryan"),
    "nihari": ("nihari", "nehari"),
    "haleem": ("haleem", "halim"),
    "pizza": ("pizza", "pizzas"),
    "dessert": ("dessert", "mithai", "sweets", "sweet"),
    "bakery": ("bakery", "bakers"),
    "cafe": ("cafe", "coffee", "chai"),
    "seafood": ("seafood", "fish", "prawn", "jhinga"),
    "afghan": ("afghan", "afghani", "kabuli"),
    "indian": ("indian",),
    "breakfast": ("breakfast", "nashta", "halwa puri"),
    "buffet": ("buffet",),
    "pakistani": ("pakistani", "desi"),
}


async def area_id_for(session: AsyncSession, phrase: str | None) -> int | None:
    """The area named in the text, or None. Matched against the `area` table itself."""
    if not phrase:
        return None
    haystack = f" {phrase.lower()} "

    rows = (await session.execute(text("SELECT id, name, name_urdu FROM area"))).all()
    # Longest name first: "North Nazimabad" must win over "Nazimabad".
    candidates: list[tuple[str, int]] = []
    for r in rows:
        for name in (r.name, r.name_urdu):
            if name:
                candidates.append((name.lower(), r.id))
    candidates.sort(key=lambda x: -len(x[0]))

    for name, area_id in candidates:
        # Word boundaries, so "Malir" does not match inside a longer word.
        if re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", haystack):
            return area_id
    return None


def cuisine_for(phrase: str | None) -> str | None:
    """The cuisine named in the text, in the spelling the `cuisines` column uses."""
    if not phrase:
        return None
    haystack = f" {phrase.lower()} "

    best: tuple[int, str] | None = None
    for canonical, aliases in CUISINE_ALIASES.items():
        for alias in aliases:
            if re.search(rf"(?<![\w]){re.escape(alias)}(?![\w])", haystack):
                # Longest alias wins, so "fast food" beats "food" if that is ever added.
                if best is None or len(alias) > best[0]:
                    best = (len(alias), canonical)
    return best[1] if best else None
