"""Load card deals from a seed file into `card_deal`. Idempotent: rows upsert on `ext_id`.

    python scripts/seed_deals.py [path/to/deals_seed.json]

Defaults to the data session's `scraper/out/deals_seed.json`. A venue row is matched on
`place_id`, then on `venue_slug`; a row whose venue is not in this database is skipped and
reported. Chain rows need no venue. The hero venue gets an HBL deal if the file gives it
none, because the stage script asks for one.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import sys

from sqlalchemy import text

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from haazir.config import settings  # noqa: E402
from haazir.db import service_session  # noqa: E402

DEFAULT = pathlib.Path(__file__).resolve().parents[2] / "scraper" / "out" / "deals_seed.json"

HERO_DEAL = {
    "id": "demo-hbl-hero",
    "bank": "HBL",
    "card_tier": "Platinum Credit",
    "discount_pct": 20,
    "max_discount_pkr": 2000,
    "days_of_week": [1, 2, 3, 4, 5, 6, 7],
    "valid_until": "2026-12-31",
    "terms": "Dine-in and takeaway. Not valid with other offers.",
    "is_demo": True,
}


async def main(data: dict | list) -> None:
    rows = data["deals"] if isinstance(data, dict) else data
    rows = list(rows)
    if not any(r.get("venue_slug") == settings.demo_hero_venue for r in rows):
        rows.append({**HERO_DEAL, "venue_slug": settings.demo_hero_venue})

    loaded, skipped = 0, []
    async with service_session() as s:
        for r in rows:
            venue_id = None
            if not r.get("chain_name"):
                venue_id = await s.scalar(
                    text("SELECT id FROM venue WHERE place_id = :p OR slug = :s "
                         "ORDER BY (place_id = :p) DESC LIMIT 1"),
                    {"p": r.get("place_id") or "", "s": r.get("venue_slug") or ""},
                )
                if venue_id is None:
                    skipped.append(r.get("id"))
                    continue
            await s.execute(
                text(
                    """
                    INSERT INTO card_deal (ext_id, bank, card_tier, venue_id, chain_name,
                                           discount_pct, max_discount_pkr, days_of_week,
                                           valid_until, terms, is_demo)
                    VALUES (:ext, :bank, :tier, :v, :chain, :pct, :cap, :days,
                            :until, :terms, :demo)
                    ON CONFLICT (ext_id) DO UPDATE SET
                        bank = EXCLUDED.bank, card_tier = EXCLUDED.card_tier,
                        venue_id = EXCLUDED.venue_id, chain_name = EXCLUDED.chain_name,
                        discount_pct = EXCLUDED.discount_pct,
                        max_discount_pkr = EXCLUDED.max_discount_pkr,
                        days_of_week = EXCLUDED.days_of_week,
                        valid_until = EXCLUDED.valid_until, terms = EXCLUDED.terms,
                        is_demo = EXCLUDED.is_demo
                    """
                ),
                {
                    "ext": r["id"], "bank": r["bank"], "tier": r.get("card_tier"),
                    "v": venue_id, "chain": r.get("chain_name"),
                    "pct": int(r["discount_pct"]), "cap": r.get("max_discount_pkr"),
                    "days": r.get("days_of_week") or [1, 2, 3, 4, 5, 6, 7],
                    "until": dt.date.fromisoformat(r["valid_until"]) if r.get("valid_until") else None, "terms": r.get("terms"),
                    "demo": bool(r.get("is_demo", True)),
                },
            )
            loaded += 1
    print(f"loaded {loaded} deals; skipped {len(skipped)} with no matching venue: {skipped}")


if __name__ == "__main__":
    source = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    asyncio.run(main(json.loads(source.read_text(encoding="utf-8"))))
