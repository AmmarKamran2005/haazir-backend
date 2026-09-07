"""Sindh Food Authority enforcement records. Plan §10.2.

The plan calls this the third load-bearing idea: trust is a bigger unsolved problem than
taste, and it has a public dataset that no consumer app in Pakistan surfaces.

**Matching is the whole risk.** A wrong match publishes "sealed for expired meat" against a
restaurant that did nothing, under its own name, on a page anyone can find. So the score is
built from two independent signals that have to agree — the name and the place — and the
0.90 threshold is enforced three times over: here, by a database CHECK constraint, and by RLS
which shows a diner only `published` rows. A human approves the last mile.

**Do not scrape Facebook.** The plan is explicit and it is right: it is fragile and against
their terms. The same actions are reported by Dawn, The News, Geo and Express Tribune, and by
the authority's own site.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("haazir.sfa")

# Below this, a record never reaches a diner. It goes to a human instead. The number is not
# a tuning knob: it is the line between a public record and a defamatory claim.
AUTO_PUBLISH_AT = 0.90

# Name similarity alone is not enough. "Student Biryani" has forty branches and a trigram
# match against any of them scores well; the area is what separates them.
W_NAME = 0.70
W_PLACE = 0.30

# Beyond this, the same name in a different neighbourhood is a different restaurant.
AREA_MATCH_KM = 4.0

EVENT_TYPES = frozenset({"sealed", "fined", "notice", "cleared", "reopened"})


@dataclass
class SfaRecord:
    """One enforcement action as extracted from a news report or the authority's own site."""

    venue_name: str
    event_type: str
    event_date: dt.date
    source_url: str
    source_name: str
    area: str | None = None
    reason: str | None = None
    fine_pkr: int | None = None
    authority: str = "Sindh Food Authority"


@dataclass
class MatchResult:
    record: SfaRecord
    venue_id: uuid.UUID | None
    venue_name: str | None
    confidence: float
    name_score: float
    place_score: float
    published: bool
    reason: str


@dataclass
class SfaReport:
    seen: int = 0
    published: int = 0
    queued_for_review: int = 0
    unmatched: int = 0
    rejected: int = 0
    reject_reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seen": self.seen,
            "auto_published": self.published,
            "queued_for_review": self.queued_for_review,
            "unmatched": self.unmatched,
            "rejected": self.rejected,
            "reject_reasons": self.reject_reasons[:20],
            "threshold": AUTO_PUBLISH_AT,
            "note": (
                "Anything below the threshold is invisible to diners until a human approves "
                "it. A wrong match is a public accusation against a real business."
            ),
        }


def validate(record: SfaRecord) -> str | None:
    """Reason to reject outright, or None."""
    if not record.venue_name or len(record.venue_name.strip()) < 3:
        return "no usable venue name"
    if record.event_type not in EVENT_TYPES:
        return f"unknown event type {record.event_type!r}"
    if not record.source_url or not record.source_url.startswith(("http://", "https://")):
        # A hygiene claim about a named business without a citation is an accusation. The
        # schema refuses to hold one and so does this.
        return "no source url"
    if record.event_date > dt.date.today() + dt.timedelta(days=1):
        return "event date is in the future"
    if record.event_date < dt.date.today() - dt.timedelta(days=365 * 5):
        return "event is more than five years old"
    return None


async def match(
    session: AsyncSession, record: SfaRecord, city: str = "Karachi"
) -> MatchResult:
    """Score this record against the venue catalogue.

    Two signals, combined. Trigram similarity on the name does the work; the area is what
    stops a chain's Gulshan branch inheriting a sealing from its Clifton one. A record with
    no area at all is capped below the auto-publish line no matter how good the name looks,
    because a 0.95 name match on "Student Biryani" identifies forty restaurants.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT v.id, v.name, a.name AS area,
                       similarity(v.name, :name) AS name_score,
                       CASE
                         WHEN :area IS NULL THEN NULL
                         WHEN a.name IS NULL THEN 0.0
                         WHEN lower(a.name) = lower(CAST(:area AS text)) THEN 1.0
                         ELSE GREATEST(0.0, 1.0 - (
                              ST_Distance(v.geom, a2.centroid) / 1000.0 / :area_km))
                       END AS place_score
                  FROM venue v
                  JOIN city c ON c.id = v.city_id
                  LEFT JOIN area a  ON a.id = v.area_id
                  LEFT JOIN area a2 ON lower(a2.name) = lower(CAST(:area AS text))
                                   AND a2.city_id = v.city_id
                 WHERE c.name = :city
                   AND v.status <> 'permanently_closed'
                   AND similarity(v.name, :name) > 0.25
                 ORDER BY similarity(v.name, :name) DESC
                 LIMIT 5
                """
            ),
            {
                "name": record.venue_name,
                "area": record.area,
                "city": city,
                "area_km": AREA_MATCH_KM,
            },
        )
    ).mappings().all()

    if not rows:
        return MatchResult(
            record=record, venue_id=None, venue_name=None, confidence=0.0,
            name_score=0.0, place_score=0.0, published=False,
            reason="no venue in the catalogue resembles this name",
        )

    best = rows[0]
    name_score = float(best["name_score"])
    has_area = record.area is not None and best["place_score"] is not None
    place_score = float(best["place_score"]) if has_area else 0.0

    if has_area:
        confidence = W_NAME * name_score + W_PLACE * place_score
    else:
        # Name only. Capped below the threshold on purpose: it is not evidence enough to
        # publish an accusation, however well the string matches.
        confidence = min(name_score * W_NAME, AUTO_PUBLISH_AT - 0.01)

    # A second candidate nearly as good means the name does not identify one restaurant.
    if len(rows) > 1 and float(rows[1]["name_score"]) > name_score - 0.08:
        confidence = min(confidence, AUTO_PUBLISH_AT - 0.01)
        ambiguity = f"; {rows[1]['name']!r} scores almost the same"
    else:
        ambiguity = ""

    confidence = round(max(0.0, min(1.0, confidence)), 4)
    published = confidence >= AUTO_PUBLISH_AT

    return MatchResult(
        record=record,
        venue_id=best["id"],
        venue_name=best["name"],
        confidence=confidence,
        name_score=round(name_score, 4),
        place_score=round(place_score, 4),
        published=published,
        reason=(
            f"name {name_score:.2f}"
            + (f", area {place_score:.2f}" if has_area else ", no area given")
            + ambiguity
        ),
    )


async def ingest(
    session: AsyncSession, records: list[SfaRecord], city: str = "Karachi"
) -> tuple[SfaReport, list[MatchResult]]:
    """Validate, match, and file. Returns the report and every match for inspection."""
    report = SfaReport()
    results: list[MatchResult] = []

    for record in records:
        report.seen += 1

        rejection = validate(record)
        if rejection:
            report.rejected += 1
            report.reject_reasons.append(f"{record.venue_name!r}: {rejection}")
            continue

        result = await match(session, record, city)
        results.append(result)

        if result.venue_id is None:
            report.unmatched += 1
        elif result.published:
            report.published += 1
        else:
            report.queued_for_review += 1

        await session.execute(
            text(
                """
                INSERT INTO regulatory_event
                       (venue_id, authority, event_type, event_date, reason, fine_pkr,
                        source_url, source_name, raw_venue_name, match_confidence, published)
                VALUES (:venue, :authority, CAST(:event_type AS reg_event_type), :event_date,
                        :reason, :fine, :url, :source_name, :raw_name, :confidence, :published)
                """
            ),
            {
                "venue": result.venue_id,
                "authority": record.authority,
                "event_type": record.event_type,
                "event_date": record.event_date,
                "reason": record.reason,
                "fine": record.fine_pkr,
                "url": record.source_url,
                "source_name": record.source_name,
                # The name as printed in the source, kept verbatim so a human reviewing the
                # match can see what was actually claimed rather than our guess about it.
                "raw_name": record.venue_name,
                "confidence": result.confidence,
                "published": result.published,
            },
        )

    log.info(
        "SFA ingest: %d seen, %d published, %d queued, %d unmatched, %d rejected",
        report.seen, report.published, report.queued_for_review,
        report.unmatched, report.rejected,
    )
    return report, results


async def review_queue(session: AsyncSession, limit: int = 50) -> list[dict]:
    """Everything awaiting a human. The last mile the threshold deliberately does not cross."""
    rows = (
        await session.execute(
            text(
                """
                SELECT e.id, e.raw_venue_name, e.event_type, e.event_date, e.reason,
                       e.fine_pkr, e.source_url, e.source_name, e.match_confidence,
                       e.created_at, v.id AS venue_id, v.name AS venue_name, a.name AS area
                  FROM regulatory_event e
                  LEFT JOIN venue v ON v.id = e.venue_id
                  LEFT JOIN area a ON a.id = v.area_id
                 WHERE NOT e.published AND e.reviewed_at IS NULL
                 ORDER BY e.match_confidence DESC, e.created_at
                 LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def decide(
    session: AsyncSession,
    event_id: uuid.UUID,
    *,
    publish: bool,
    reviewer_id: uuid.UUID,
    venue_id: uuid.UUID | None = None,
) -> dict | None:
    """A human's decision. Publishing sets confidence to 1.0, because it is now a person's
    judgement rather than a similarity score, and the CHECK constraint would otherwise refuse
    a record a reviewer has personally confirmed."""
    row = (
        await session.execute(
            text(
                """
                UPDATE regulatory_event
                   SET published = :publish,
                       venue_id = COALESCE(:venue, venue_id),
                       match_confidence = CASE WHEN :publish THEN 1.0 ELSE match_confidence END,
                       reviewed_by = :reviewer,
                       reviewed_at = now()
                 WHERE id = :id
             RETURNING id, published, venue_id, raw_venue_name, match_confidence
                """
            ),
            {"id": event_id, "publish": publish, "reviewer": reviewer_id, "venue": venue_id},
        )
    ).mappings().first()
    return dict(row) if row else None
