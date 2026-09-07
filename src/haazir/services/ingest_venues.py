"""Load the scraped Karachi dataset. Plan §10.1.

Reads the JSONL that `scraper/out/` produces and writes venues, menus, price history, reviews
and occupancy priors. Idempotent: upserts on `place_id` for venues and on
`(venue_id, dish_id)` for menu lines, so a re-run after a bigger scrape adds and corrects
rather than duplicating.

Three things here are decisions rather than plumbing.

**Attributes get a confidence, not a bare boolean.** §3.4 says every access fact carries a
value, a confidence, a count and a date. Google's attributes are reasonably reliable and
nobody has stood in the restaurant and checked them, so they land at 0.75 with `n = 0`. A
diner confirming one later raises it. A `null` from the scraper is dropped entirely rather
than stored as `{"v": null}`: an absent key means "we do not know", and that is different
from a recorded "no".

**Review prose is read once and thrown away.** §3.7 keeps features and a vector, never the
text. The scraper already hashes author names, so no name reaches this process at all.

**Every venue gets 168 occupancy priors, from Google where it exists and an archetype where
it does not**, and `source` records which. A prior presented as a measurement is the exact
thing this product exists not to do.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .normalise import canonicalise_dish, normalise_phone, slugify

log = logging.getLogger("haazir.ingest")

# hour_of_week is weekday * 24 + hour, with weekday 0 = Sunday. That matches Postgres
# EXTRACT(DOW) and the dayFactor array in app/assets/js/data.js, so the estimator, the
# database and the prototype all index the week the same way.
WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
DAY_FACTOR = (1.02, 0.78, 0.83, 0.87, 0.94, 1.12, 1.16)  # Sun..Sat, Karachi peaks Fri/Sat

# The eight curves from app/assets/js/data.js. 24 hourly values, 0..1, average day.
ARCHETYPES: dict[str, tuple[float, ...]] = {
    "nihari_morning": (.10,.06,.04,.03,.06,.22,.62,.88,.94,.86,.66,.48,.40,.36,.30,.28,.32,.38,.44,.50,.52,.46,.32,.18),
    "bbq_night":      (.30,.16,.07,.03,.02,.02,.03,.05,.08,.10,.13,.20,.34,.40,.33,.28,.32,.44,.58,.74,.88,.94,.86,.60),
    "seafood_view":   (.18,.09,.04,.02,.02,.02,.03,.04,.06,.08,.11,.18,.30,.34,.28,.24,.30,.46,.66,.84,.92,.90,.74,.42),
    "biryani_lunch":  (.08,.04,.03,.02,.03,.06,.14,.24,.30,.34,.42,.62,.88,.92,.74,.52,.44,.46,.54,.62,.66,.54,.32,.16),
    "cafe_day":       (.12,.06,.03,.02,.02,.03,.06,.14,.28,.44,.58,.66,.70,.72,.68,.66,.70,.76,.80,.78,.70,.58,.38,.22),
    "bakery_evening": (.10,.05,.03,.02,.02,.03,.08,.18,.28,.34,.38,.42,.46,.48,.50,.56,.66,.78,.86,.88,.80,.66,.44,.22),
    "street_late":    (.46,.30,.16,.06,.03,.02,.03,.05,.08,.12,.16,.22,.30,.34,.30,.28,.34,.44,.56,.70,.82,.90,.92,.72),
    "fastfood_all":   (.34,.20,.10,.04,.03,.03,.05,.10,.18,.26,.36,.48,.62,.64,.54,.48,.52,.62,.74,.82,.86,.84,.72,.52),
}

# A Google aggregate is smoothed over weeks and is a decent baseline. An archetype is a guess
# about a category, so it is admitted as one: nearly twice the uncertainty.
SIGMA_GOOGLE = 0.12
SIGMA_ARCHETYPE = 0.22

# Google reports attributes it has, not attributes a diner confirmed on the premises.
ATTR_CONFIDENCE = 0.75

# Facts Google cannot answer. The scraper leaves them null; this list documents that they are
# deliberately absent and awaiting the human-verified pipeline, not simply missed.
NEVER_FROM_GOOGLE = ("prayer_area", "family_section", "high_chairs", "generator_backup")


@dataclass
class IngestReport:
    venues_seen: int = 0
    venues_written: int = 0
    venues_rejected: int = 0
    reject_reasons: Counter = field(default_factory=Counter)
    areas_by_name: int = 0
    areas_by_distance: int = 0
    areas_unresolved: int = 0
    priors_from_google: int = 0
    priors_from_archetype: int = 0
    dishes_created: int = 0
    menu_lines: int = 0
    price_rows: int = 0
    reviews: int = 0
    orphan_dishes_removed: int = 0
    stale_menu_lines_removed: int = 0

    @property
    def reject_rate(self) -> float:
        return self.venues_rejected / self.venues_seen if self.venues_seen else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "venues_seen": self.venues_seen,
            "venues_written": self.venues_written,
            "venues_rejected": self.venues_rejected,
            "reject_rate": round(self.reject_rate, 4),
            "reject_reasons": dict(self.reject_reasons),
            "area_resolution": {
                "by_name": self.areas_by_name,
                "by_nearest_centroid": self.areas_by_distance,
                "unresolved": self.areas_unresolved,
            },
            "occupancy_priors": {
                "from_google_popular_times": self.priors_from_google,
                "from_archetype": self.priors_from_archetype,
            },
            "dishes_created": self.dishes_created,
            "menu_lines": self.menu_lines,
            "price_rows": self.price_rows,
            "reviews": self.reviews,
            "orphan_dishes_removed": self.orphan_dishes_removed,
            "stale_menu_lines_removed": self.stale_menu_lines_removed,
        }


# --- helpers -----------------------------------------------------------------


def pick_archetype(venue_type: str | None, cuisines: list[str] | None) -> str:
    """Choose a demand curve from what the venue sells and what kind of place it is.

    Cuisine wins over venue type, because a nihari house and a burger joint are both
    `restaurant` and their days look nothing alike.
    """
    c = {x.lower() for x in (cuisines or [])}
    if {"nihari", "breakfast", "halwa puri"} & c:
        return "nihari_morning"
    if "seafood" in c:
        return "seafood_view"
    if "bbq" in c or "barbecue" in c:
        return "bbq_night"
    if "biryani" in c:
        return "biryani_lunch"

    match (venue_type or "").lower():
        case "cafe":
            return "cafe_day"
        case "bakery" | "dessert" | "sweets":
            return "bakery_evening"
        case "street_food":
            return "street_late"
        case "fast_food":
            return "fastfood_all"
        case _:
            return "bbq_night" if "pakistani" in c else "cafe_day"


def build_attributes(raw: dict | None, scraped_at: str | None) -> dict:
    """Flat booleans from the scraper to the `{v, c, n, at}` contract of §3.4."""
    if not raw:
        return {}
    at = (scraped_at or dt.date.today().isoformat())[:10]
    out: dict[str, dict] = {}
    for key, value in raw.items():
        if value is None:  # unknown is an absent key, never a recorded "no"
            continue
        out[key] = {"v": value, "c": ATTR_CONFIDENCE, "n": 0, "at": at, "src": "places_api"}
    return out


def priors_for(record: dict) -> tuple[list[tuple[int, float, float]], str]:
    """168 `(hour_of_week, mean_ratio, sigma)` rows, and where they came from."""
    popular = record.get("popular_times")
    rows: list[tuple[int, float, float]] = []

    if isinstance(popular, dict) and any(popular.get(d) for d in WEEKDAYS):
        for weekday, day in enumerate(WEEKDAYS):
            series = popular.get(day) or []
            for hour in range(24):
                raw = series[hour] if hour < len(series) else None
                ratio = max(0.0, min(1.0, (raw or 0) / 100.0))
                rows.append((weekday * 24 + hour, ratio, SIGMA_GOOGLE))
        return rows, "google_popular_times"

    curve = ARCHETYPES[pick_archetype(record.get("venue_type"), record.get("cuisines"))]
    for weekday in range(7):
        for hour in range(24):
            ratio = max(0.0, min(1.0, curve[hour] * DAY_FACTOR[weekday]))
            rows.append((weekday * 24 + hour, ratio, SIGMA_ARCHETYPE))
    return rows, "archetype"


def _valid(record: dict) -> str | None:
    """Reason to reject, or None to keep."""
    if not record.get("place_id"):
        return "no place_id"
    if not record.get("name"):
        return "no name"
    lat, lng = record.get("lat"), record.get("lng")
    if lat is None or lng is None:
        return "no coordinates"
    # Karachi's bounding box, generously drawn. A venue outside it is a bad geocode, and one
    # bad geocode puts a restaurant in the Arabian Sea on the city map.
    if not (24.6 <= float(lat) <= 25.3 and 66.8 <= float(lng) <= 67.6):
        return "coordinates outside Karachi"
    if record.get("permanently_closed"):
        return "permanently closed"
    return None


# --- venues ------------------------------------------------------------------


async def _area_lookup(session: AsyncSession, city_id: int) -> dict[str, int]:
    rows = await session.execute(
        text("SELECT id, name FROM area WHERE city_id = :c"), {"c": city_id}
    )
    return {name: aid for aid, name in rows}


async def load_venues(
    session: AsyncSession, records: list[dict], *, city_name: str = "Karachi"
) -> IngestReport:
    """Bulk path, deliberately.

    The obvious shape is a loop that upserts one venue, inserts its provenance row and writes
    its 168 priors. That is three round trips per venue, and with the database in Singapore
    each one costs roughly 200 ms: 1,700 venues would take over an hour, which turns the whole
    dataset into something you run overnight instead of something you iterate on. Every
    statement below is an `executemany` over the whole set, so the load costs a handful of
    round trips regardless of how many venues arrive.
    """
    report = IngestReport()

    city_id = await session.scalar(
        text("SELECT id FROM city WHERE name = :n"), {"n": city_name}
    )
    if city_id is None:
        raise RuntimeError(f"city {city_name!r} is not seeded; run `alembic upgrade head`")
    areas = await _area_lookup(session, city_id)

    prepared: list[dict] = []
    provenance: list[dict] = []
    needs_geo_area: list[dict] = []

    for record in records:
        report.venues_seen += 1
        reason = _valid(record)
        if reason:
            report.venues_rejected += 1
            report.reject_reasons[reason] += 1
            continue

        lat, lng = float(record["lat"]), float(record["lng"])
        row = {
            "place_id": record["place_id"],
            "slug": slugify(record["name"], record["place_id"][-6:].lower()),
            "name": record["name"],
            "name_urdu": record.get("name_urdu"),
            "aliases": record.get("aliases") or [],
            "brand": record.get("brand"),
            "branch_label": record.get("branch_label"),
            "city_id": city_id,
            # Area by canonical name first. Both sides draw from the same list, so this is the
            # common path; nearest-centroid below is the fallback for a name that drifted.
            "area_id": areas.get(record.get("area") or ""),
            "lat": lat,
            "lng": lng,
            "address_full": record.get("address_full"),
            "phone": normalise_phone(record.get("phone")),
            "whatsapp": normalise_phone(record.get("whatsapp")),
            "website": record.get("website"),
            "instagram": record.get("instagram"),
            "maps_url": record.get("maps_url"),
            "venue_type": record.get("venue_type") or "restaurant",
            "cuisines": record.get("cuisines") or [],
            "price_level": record.get("price_level"),
            "avg_ticket_pkr": record.get("avg_ticket_pkr"),
            "capacity_covers": record.get("seating_capacity"),
            "google_rating": record.get("google_rating"),
            "google_review_count": record.get("google_review_count"),
            "rating_histogram": _json(record.get("rating_histogram")),
            "hours": _json(record.get("hours")),
            "hours_ramadan": _json(record.get("hours_ramadan")),
            "popular_times": _json(record.get("popular_times")),
            "attributes": _json(
                build_attributes(record.get("attributes"), record.get("scraped_at"))
            ),
            "photos": _json(record.get("photos") or []),
            "blurb": record.get("editorial_summary"),
            "status": (
                "temporarily_closed" if record.get("temporarily_closed") else "active"
            ),
        }
        if row["area_id"] is not None:
            report.areas_by_name += 1
        else:
            needs_geo_area.append(row)

        prepared.append(row)
        provenance.append(
            {
                "place_id": record["place_id"],
                "src": record.get("source") or "places_api",
                "url": record.get("source_url"),
                "at": _ts(record.get("scraped_at")),
                "conf": record.get("extraction_confidence") or 1.0,
                "record": record,
            }
        )

    if not prepared:
        return report

    # Areas that missed by name, resolved in one round trip rather than one apiece.
    if needs_geo_area:
        resolved = await session.execute(
            text(
                """
                SELECT p.idx,
                       (SELECT a.id FROM area a
                         WHERE a.city_id = :city
                         ORDER BY a.centroid <-> ST_SetSRID(
                             ST_MakePoint(p.lng, p.lat), 4326)::geography
                         LIMIT 1) AS area_id
                  FROM unnest(CAST(:idx AS int[]), CAST(:lngs AS float8[]),
                              CAST(:lats AS float8[])) AS p(idx, lng, lat)
                """
            ),
            {
                "city": city_id,
                "idx": list(range(len(needs_geo_area))),
                "lngs": [r["lng"] for r in needs_geo_area],
                "lats": [r["lat"] for r in needs_geo_area],
            },
        )
        for idx, area_id in resolved:
            needs_geo_area[idx]["area_id"] = area_id
            if area_id is None:
                report.areas_unresolved += 1
            else:
                report.areas_by_distance += 1

    await session.execute(text(_VENUE_UPSERT), prepared)
    report.venues_written = len(prepared)

    ids = dict(
        (
            await session.execute(
                text(
                    "SELECT place_id, id FROM venue "
                    "WHERE place_id = ANY(CAST(:p AS text[]))"
                ),
                {"p": [r["place_id"] for r in prepared]},
            )
        ).all()
    )

    await session.execute(
        text(
            """
            INSERT INTO venue_source (venue_id, source, source_url, scraped_at, confidence)
            VALUES (:v, :src, :url, COALESCE(:at, now()), :conf)
            """
        ),
        [
            {
                "v": ids[p["place_id"]],
                "src": p["src"],
                "url": p["url"],
                "at": p["at"],
                "conf": p["conf"],
            }
            for p in provenance
            if p["place_id"] in ids
        ],
    )

    prior_batch: list[dict] = []
    for p in provenance:
        venue_id = ids.get(p["place_id"])
        if venue_id is None:
            continue
        rows, source = priors_for(p["record"])
        if source == "google_popular_times":
            report.priors_from_google += 1
        else:
            report.priors_from_archetype += 1
        prior_batch.extend(
            {"v": venue_id, "h": h, "m": m, "s": s, "src": source} for h, m, s in rows
        )
        if len(prior_batch) >= 20_000:
            await _flush_priors(session, prior_batch)
            prior_batch.clear()
    if prior_batch:
        await _flush_priors(session, prior_batch)

    return report


_VENUE_UPSERT = """
    INSERT INTO venue (
        place_id, slug, name, name_urdu, aliases, brand, branch_label,
        city_id, area_id, geom, address_full, phone, whatsapp, website,
        instagram, maps_url, venue_type, cuisines, price_level,
        avg_ticket_pkr, capacity_covers, google_rating, google_review_count,
        rating_histogram, hours, hours_ramadan, popular_times, attributes,
        photos, blurb, tier, status)
    VALUES (
        :place_id, :slug, :name, :name_urdu, CAST(:aliases AS text[]), :brand,
        :branch_label, :city_id, :area_id,
        ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
        :address_full, :phone, :whatsapp, :website, :instagram, :maps_url,
        :venue_type, CAST(:cuisines AS text[]), :price_level, :avg_ticket_pkr,
        :capacity_covers, :google_rating, :google_review_count,
        CAST(:rating_histogram AS jsonb), CAST(:hours AS jsonb),
        CAST(:hours_ramadan AS jsonb), CAST(:popular_times AS jsonb),
        CAST(:attributes AS jsonb), CAST(:photos AS jsonb), :blurb,
        'seeded', CAST(:status AS venue_status))
    ON CONFLICT (place_id) DO UPDATE SET
        name = EXCLUDED.name,
        area_id = EXCLUDED.area_id,
        geom = EXCLUDED.geom,
        address_full = EXCLUDED.address_full,
        phone = COALESCE(EXCLUDED.phone, venue.phone),
        website = COALESCE(EXCLUDED.website, venue.website),
        maps_url = EXCLUDED.maps_url,
        venue_type = EXCLUDED.venue_type,
        cuisines = EXCLUDED.cuisines,
        price_level = COALESCE(EXCLUDED.price_level, venue.price_level),
        google_rating = EXCLUDED.google_rating,
        google_review_count = EXCLUDED.google_review_count,
        hours = COALESCE(EXCLUDED.hours, venue.hours),
        popular_times = COALESCE(EXCLUDED.popular_times, venue.popular_times),
        -- Scraped attributes merge UNDER whatever is already there, so an owner edit or a
        -- crowd verification is never overwritten by the next scrape (plan 10.1 rule 6).
        attributes = EXCLUDED.attributes || venue.attributes,
        photos = EXCLUDED.photos,
        status = EXCLUDED.status
"""


async def _flush_priors(session: AsyncSession, batch: list[dict]) -> None:
    await session.execute(
        text(
            """
            INSERT INTO occupancy_prior (venue_id, hour_of_week, mean_ratio, sigma, source)
            VALUES (:v, :h, :m, :s, :src)
            ON CONFLICT (venue_id, hour_of_week) DO UPDATE SET
                mean_ratio = EXCLUDED.mean_ratio,
                sigma = EXCLUDED.sigma,
                source = EXCLUDED.source
            """
        ),
        batch,
    )


# --- menus -------------------------------------------------------------------


async def load_menu_items(
    session: AsyncSession, records: list[dict], report: IngestReport | None = None
) -> IngestReport:
    """Bulk, for the same reason `load_venues` is: this is the largest table by row count and
    a per-item loop spends the entire load waiting on the network."""
    report = report or IngestReport()

    venue_ids = dict(
        (
            await session.execute(
                text("SELECT place_id, id FROM venue WHERE place_id IS NOT NULL")
            )
        ).all()
    )

    parsed_rows: list[tuple[Any, Any, dict]] = []
    dishes: dict[str, dict] = {}

    for record in records:
        venue_id = venue_ids.get(record.get("place_id") or "")
        if venue_id is None:
            continue
        # Scraping spec section 6: below 0.7 goes to human review, never the main table.
        if (record.get("extraction_confidence") or 1.0) < 0.7:
            continue

        parsed = canonicalise_dish(record.get("name") or "")
        if parsed is None:
            continue

        dishes.setdefault(
            parsed.normalized,
            {
                "canonical": parsed.canonical,
                "norm": parsed.normalized,
                "family": parsed.family,
                "protein": parsed.protein,
                "urdu": record.get("name_urdu"),
                "category": record.get("section"),
            },
        )
        parsed_rows.append((venue_id, parsed, record))

    if not dishes:
        return report

    await session.execute(
        text(
            """
            INSERT INTO dish (canonical_name, name_normalized, family, protein,
                              name_urdu, category)
            VALUES (:canonical, :norm, :family, :protein, :urdu, :category)
            ON CONFLICT (name_normalized) DO UPDATE SET
                family = EXCLUDED.family,
                protein = EXCLUDED.protein,
                name_urdu = COALESCE(EXCLUDED.name_urdu, dish.name_urdu)
            """
        ),
        list(dishes.values()),
    )
    report.dishes_created += len(dishes)

    dish_ids = dict(
        (
            await session.execute(
                text(
                    "SELECT name_normalized, id FROM dish "
                    "WHERE name_normalized = ANY(CAST(:n AS text[]))"
                ),
                {"n": list(dishes)},
            )
        ).all()
    )

    menu_rows: list[dict] = []
    price_rows: list[dict] = []
    for venue_id, parsed, record in parsed_rows:
        dish_id = dish_ids.get(parsed.normalized)
        if dish_id is None:
            continue
        price = record.get("price_pkr")
        seen_at = _ts(record.get("price_seen_at"))
        menu_rows.append(
            {
                "v": venue_id,
                "d": dish_id,
                "menu_name": record.get("name"),
                "section": record.get("section"),
                "description": record.get("description"),
                "price": price,
                "unit": record.get("price_unit") or parsed.price_unit or "per_plate",
                "seen_at": seen_at,
                "source": record.get("source"),
            }
        )
        if price:
            price_rows.append(
                {
                    "v": venue_id,
                    "d": dish_id,
                    "price": price,
                    "seen_at": seen_at,
                    "src": record.get("source") or "scrape",
                }
            )

    for i in range(0, len(menu_rows), 2_000):
        await session.execute(
            text(
                """
                INSERT INTO venue_dish (venue_id, dish_id, menu_name, section, description,
                                        price_pkr, price_unit, price_seen_at, price_source)
                VALUES (:v, :d, :menu_name, :section, :description, :price,
                        CAST(:unit AS price_unit), :seen_at, :source)
                ON CONFLICT (venue_id, dish_id) DO UPDATE SET
                    menu_name = EXCLUDED.menu_name,
                    price_pkr = COALESCE(EXCLUDED.price_pkr, venue_dish.price_pkr),
                    price_seen_at = EXCLUDED.price_seen_at,
                    price_source = EXCLUDED.price_source
                """
            ),
            menu_rows[i : i + 2_000],
        )
    report.menu_lines += len(menu_rows)

    # Append-only price history. Under this much food inflation a price with no date is worse
    # than no price, so each observation is kept rather than overwriting the last.
    for i in range(0, len(price_rows), 2_000):
        await session.execute(
            text(
                """
                INSERT INTO venue_dish_price (venue_id, dish_id, price_pkr, seen_at, source)
                SELECT :v, :d, :price, COALESCE(:seen_at, now()), :src
                 WHERE NOT EXISTS (
                    SELECT 1 FROM venue_dish_price
                     WHERE venue_id = :v AND dish_id = :d AND price_pkr = :price
                 )
                """
            ),
            price_rows[i : i + 2_000],
        )
    report.price_rows += len(price_rows)

    # Menu lines for these venues that this load did not write. The scraper's menu is the
    # whole menu for a venue it covered, so anything left over is either a dish that came off
    # the menu or, more often, the same dish under an identity a previous normalisation rule
    # produced. Leaving those in place made one physical item appear twice, which quietly
    # double-counts the venue in every price comparison it takes part in.
    stale = await session.execute(
        text(
            """
            DELETE FROM venue_dish vd
             WHERE vd.venue_id = ANY(CAST(:venues AS uuid[]))
               AND NOT EXISTS (
                   SELECT 1 FROM unnest(CAST(:vids AS uuid[]), CAST(:dids AS uuid[]))
                        AS kept(venue_id, dish_id)
                    WHERE kept.venue_id = vd.venue_id AND kept.dish_id = vd.dish_id
               )
            """
        ),
        {
            "venues": list({r["v"] for r in menu_rows}),
            "vids": [r["v"] for r in menu_rows],
            "dids": [r["d"] for r in menu_rows],
        },
    )
    report.stale_menu_lines_removed += stale.rowcount or 0

    # Dishes nothing points at any more. Changing a normalisation rule re-canonicalises every
    # menu line and leaves the old dish rows behind unreferenced; they then show up in
    # `/dishes/search` as families no venue sells. Cheap to sweep, and confusing to leave.
    removed = await session.execute(
        text(
            "DELETE FROM dish WHERE NOT EXISTS "
            "(SELECT 1 FROM venue_dish vd WHERE vd.dish_id = dish.id)"
        )
    )
    report.orphan_dishes_removed += removed.rowcount or 0

    return report


# --- reviews -----------------------------------------------------------------


async def load_reviews(
    session: AsyncSession, records: list[dict], report: IngestReport | None = None
) -> IngestReport:
    """Features in, prose dropped. §3.7: no review text is ever stored."""
    report = report or IngestReport()

    venue_ids = dict(
        (
            await session.execute(
                text("SELECT place_id, id FROM venue WHERE place_id IS NOT NULL")
            )
        ).all()
    )

    batch = []
    for record in records:
        venue_id = venue_ids.get(record.get("place_id") or "")
        rating = record.get("rating")
        if venue_id is None or not rating:
            continue
        batch.append({
            "v": venue_id,
            "ext": record.get("review_id"),
            "rating": int(rating),
            "lang": record.get("language"),
            "posted": _ts(record.get("posted_at")),
            # Already a hash when it arrives. No reviewer name enters this process.
            "author": record.get("author_hash") or "",
            "guide": record.get("is_local_guide"),
            "photos": record.get("photo_count"),
        })

    for i in range(0, len(batch), 2_000):
        chunk = batch[i : i + 2_000]
        await session.execute(
            text(
                """
                INSERT INTO review_sample (venue_id, external_id, rating, lang, posted_at,
                                           author_hash, is_local_guide, photo_count)
                VALUES (:v, :ext, :rating, :lang, :posted, :author, :guide, :photos)
                ON CONFLICT (venue_id, external_id) DO NOTHING
                """
            ),
            chunk,
        )
        report.reviews += len(chunk)

    return report


def _json(value: Any) -> str | None:
    import json

    return None if value is None else json.dumps(value, ensure_ascii=False)


def _ts(value: Any) -> dt.datetime | None:
    """ISO string to an aware datetime, or None.

    Parsed here rather than handed to Postgres as `CAST(:x AS timestamptz)`, because asyncpg
    infers a parameter's type from the cast target and then refuses the Python `str` outright.
    Doing it in Python also means a malformed date becomes a null we can count, instead of an
    exception that stops the whole load on one bad row.
    """
    if value is None or isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min, tzinfo=dt.UTC)
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
