"""Card deals: which ones apply to a venue, and the list endpoint's query.

A chain deal matches any venue whose name starts with the chain's name, resolved here at read
time. One definition, used by the venue card, the list endpoint and the chat agent, so they
cannot disagree about whether a branch is covered.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .clock import local_now

# Banks as a diner types them, to the name stored on the deal.
BANK_ALIASES = {
    "hbl": "HBL", "habib": "HBL",
    "meezan": "Meezan Bank",
    "ubl": "UBL",
    "alfalah": "Bank Alfalah", "bafl": "Bank Alfalah",
    "mcb": "MCB",
    "standard chartered": "Standard Chartered", "scb": "Standard Chartered", "sc": "Standard Chartered",
    "faysal": "Faysal Bank",
    "js": "JS Bank", "jsbl": "JS Bank",
}

# `deal d` applies to `venue v`.
APPLIES_SQL = (
    "(d.venue_id = v.id OR (d.chain_name IS NOT NULL AND v.name ILIKE d.chain_name || '%'))"
)


def canonical_bank(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip().lower().replace(" bank", "").strip()
    return BANK_ALIASES.get(key, raw.strip())


def _today_iso() -> int:
    return local_now().isoweekday()


async def for_venues(session: AsyncSession, venue_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict]]:
    """`{venue_id: [{bank, discount_pct, cap, card_tier, today}]}`, best discount first."""
    if not venue_ids:
        return {}
    rows = (
        await session.execute(
            text(
                f"""
                SELECT v.id AS venue_id, d.bank, d.card_tier, d.discount_pct,
                       d.max_discount_pkr, d.days_of_week
                  FROM venue v JOIN card_deal d ON {APPLIES_SQL}
                 WHERE v.id = ANY(CAST(:ids AS uuid[]))
                   AND (d.valid_until IS NULL OR d.valid_until >= CURRENT_DATE)
                 ORDER BY d.discount_pct DESC
                """
            ),
            {"ids": [str(v) for v in venue_ids]},
        )
    ).mappings().all()
    today = _today_iso()
    out: dict[uuid.UUID, list[dict]] = {}
    for r in rows:
        out.setdefault(r["venue_id"], []).append(
            {
                "bank": r["bank"],
                "card_tier": r["card_tier"],
                "discount_pct": r["discount_pct"],
                "cap": r["max_discount_pkr"],
                "today": today in (r["days_of_week"] or []),
            }
        )
    return out


async def search(
    session: AsyncSession,
    *,
    bank: str | None = None,
    area: str | None = None,
    venue: str | None = None,
    include_demo_flag: bool = False,
    limit: int = 100,
) -> list[dict]:
    """The `/deals` list. A chain deal is expanded to the matching venues in the area asked
    about, so every row the client gets names a restaurant it can open."""
    bank = canonical_bank(bank)
    rows = (
        await session.execute(
            text(
                f"""
                SELECT d.id, d.bank, d.card_tier, d.chain_name, d.discount_pct,
                       d.max_discount_pkr, d.days_of_week, d.valid_until, d.terms, d.is_demo,
                       v.id AS venue_id, v.slug, v.name AS venue_name, a.name AS area
                  FROM card_deal d
                  JOIN venue v ON {APPLIES_SQL}
                  LEFT JOIN area a ON a.id = v.area_id
                 WHERE (d.valid_until IS NULL OR d.valid_until >= CURRENT_DATE)
                   AND v.status = 'active'
                   AND (CAST(:bank AS text) IS NULL OR lower(d.bank) = lower(:bank))
                   AND (CAST(:area AS text) IS NULL OR a.name ILIKE :area_like)
                   AND (CAST(:venue AS text) IS NULL OR v.slug = :venue
                        OR CAST(v.id AS text) = :venue)
                 ORDER BY d.discount_pct DESC, v.google_review_count DESC NULLS LAST
                 LIMIT :limit
                """
            ),
            {"bank": bank, "area": area, "area_like": f"%{area}%" if area else None,
             "venue": venue, "limit": limit},
        )
    ).mappings().all()
    today = _today_iso()
    return [
        {
            "id": r["id"],
            "bank": r["bank"],
            "card_tier": r["card_tier"],
            "discount_pct": r["discount_pct"],
            "cap": r["max_discount_pkr"],
            "max_discount_pkr": r["max_discount_pkr"],
            "days_of_week": r["days_of_week"],
            "today": today in (r["days_of_week"] or []),
            "valid_until": r["valid_until"],
            "terms": r["terms"],
            "chain_name": r["chain_name"],
            "venue": {"id": str(r["venue_id"]), "slug": r["slug"], "name": r["venue_name"],
                      "area": r["area"]},
            **({"is_demo": r["is_demo"]} if include_demo_flag else {}),
        }
        for r in rows
    ]
