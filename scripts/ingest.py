"""Load a scraper output directory into the database.

    api/.venv/Scripts/python scripts/ingest.py ../scraper/out
    api/.venv/Scripts/python scripts/ingest.py ../scraper/out --limit 50   # dry-ish run

Runs against whatever `DATABASE_URL` names, so check it before pointing this at anything.
Everything is upserted, so re-running after a bigger scrape corrects and extends rather than
duplicating.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from haazir.db import service_session  # noqa: E402
from haazir.services import ingest_venues as ingest  # noqa: E402


def read_jsonl(path: pathlib.Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
            if limit and len(out) >= limit:
                break
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", type=pathlib.Path)
    ap.add_argument("--limit", type=int, default=None, help="only the first N venues")
    ap.add_argument("--skip-reviews", action="store_true")
    args = ap.parse_args()

    venues = read_jsonl(args.out_dir / "venues.jsonl", args.limit)
    if not venues:
        print(f"no venues.jsonl under {args.out_dir}", file=sys.stderr)
        return 1
    keep = {v["place_id"] for v in venues if v.get("place_id")}
    menus = [m for m in read_jsonl(args.out_dir / "menu_items.jsonl") if m.get("place_id") in keep]
    reviews = (
        []
        if args.skip_reviews
        else [r for r in read_jsonl(args.out_dir / "reviews.jsonl") if r.get("place_id") in keep]
    )

    print(f"venues {len(venues)}  menu items {len(menus)}  reviews {len(reviews)}\n")

    started = time.perf_counter()
    async with service_session() as session:
        report = await ingest.load_venues(session, venues)
        print(f"  venues      {report.venues_written:6} written, "
              f"{report.venues_rejected} rejected ({report.reject_rate:.1%})")

        await ingest.load_menu_items(session, menus, report)
        print(f"  menu        {report.menu_lines:6} lines, {report.dishes_created} dishes")

        if reviews:
            await ingest.load_reviews(session, reviews, report)
            print(f"  reviews     {report.reviews:6} feature rows (no text stored)")

    print(f"\n{json.dumps(report.as_dict(), indent=2)}")
    print(f"\ndone in {time.perf_counter() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
