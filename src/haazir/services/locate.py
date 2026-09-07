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
from difflib import SequenceMatcher

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

    # Nothing exact. Try it as a misspelling before giving up.
    return _best_fuzzy(candidates, phrase)


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
    if best:
        return best[1]

    # Nothing exact — "chineese", "biryni". Match every alias fuzzily and take the best.
    aliases = [(a, c) for c, group in CUISINE_ALIASES.items() for a in group]
    return _best_fuzzy(aliases, phrase)


# A time the diner actually typed, as opposed to the default every client sends. Used to
# decide whether a travel cap is theirs or ours.
_TIME_RE = re.compile(r"\d{1,3}\s*(min|minute|mint|minat|ghant|hour|hr)", re.I)


def mentions_a_time(phrase: str | None) -> bool:
    return bool(phrase and _TIME_RE.search(phrase))


# ── spelling ──────────────────────────────────────────────────────────────────
#
# People type "nazimbad", "chineese", "clifon". An exact scan finds none of them and the
# diner gets an unfiltered list with no hint that their word was thrown away.
#
# Word-level rather than whole-phrase: "biryani in north nazimbad" against "North Nazimabad"
# scores badly as one string and perfectly word by word. Every word of the area name has to
# find a partner, so "Nazimabad" cannot quietly satisfy "North Nazimabad".

_MIN_WORD = 0.78   # per word: "nazimbad" vs "nazimabad" is 0.94, "saddar" vs "sadar" is 0.91
_MIN_MEAN = 0.84   # across the name, so one strong word cannot carry a wrong one


def _fuzzy_hit(name: str, words: list[str]) -> float:
    """Mean similarity of the name's words to their best partners, or 0 if any is too weak."""
    parts = [w for w in re.split(r"[\s\-]+", name.lower()) if len(w) > 2]
    if not parts:
        return 0.0
    scores = []
    for part in parts:
        best = max((SequenceMatcher(None, part, w).ratio() for w in words), default=0.0)
        if best < _MIN_WORD:
            return 0.0
        scores.append(best)
    return sum(scores) / len(scores)


def _best_fuzzy(candidates: list[tuple[str, object]], phrase: str) -> object | None:
    words = [w for w in re.split(r"[^\w]+", phrase.lower()) if len(w) > 2]
    if not words:
        return None
    best, best_score = None, 0.0
    for name, value in candidates:
        score = _fuzzy_hit(name, words)
        if score > best_score:
            best, best_score = value, score
    return best if best_score >= _MIN_MEAN else None


# ── the model, last ───────────────────────────────────────────────────────────


async def resolve(session: AsyncSession, phrase: str | None) -> dict:
    """`{area_id, cuisine, dish}` from a sentence: exact, then fuzzy, then a model.

    The order is the point. Exact and fuzzy are free, instant and cannot invent anything, and
    they answer almost every real query. A model is asked only about the sentences those two
    could not read at all — and it is handed the closed lists and checked against them, so the
    worst it can do is return nothing.
    """
    from . import llm

    area_id = await area_id_for(session, phrase)
    cuisine = cuisine_for(phrase)
    out = {"area_id": area_id, "cuisine": cuisine, "dish": None, "by": "rules"}

    if area_id is not None or cuisine or not phrase or not llm.available():
        return out

    rows = (await session.execute(text("SELECT id, name FROM area"))).all()
    names = {r.name: r.id for r in rows}
    cuisines = sorted(CUISINE_ALIASES)

    guess = await llm.extract_intent(phrase, list(names), cuisines)
    if guess["area"]:
        out["area_id"] = names.get(guess["area"])
    out["cuisine"] = guess["cuisine"]
    out["dish"] = guess["dish"]
    if out["area_id"] is not None or out["cuisine"] or out["dish"]:
        out["by"] = "model"
    return out
