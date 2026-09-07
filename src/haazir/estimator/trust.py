"""Trust score. Port of `trustBreakdown` in `app/assets/js/engine.js`. Plan §6.5.

A named linear combination out of 100, and the components are the score rather than a
decomposition of it. That matters for the same reason the fusion weights are shown: a venue
that wants to know why it scored 61 gets five numbers and their maxima, not a sentence
generated about the total. It also means a venue can see exactly what to fix.

The regulatory component is the one with teeth. A sealing costs 36 of the 40 available points
on the day it happens and recovers at about a third of a point a day, so a venue sealed two
years ago is not permanently branded, and one sealed last week cannot buy its way back with
review volume. §14 rule 6: a later `cleared` or `reopened` event is displayed as prominently
as the sealing, and it lifts the score here too.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass(frozen=True, slots=True)
class Component:
    key: str
    pts: int
    max: int
    note: str

    def as_dict(self) -> dict:
        return {"key": self.key, "pts": self.pts, "max": self.max, "note": self.note}


@dataclass(frozen=True, slots=True)
class TrustScore:
    total: int
    components: list[Component]

    def as_dict(self) -> dict:
        return {"total": self.total, "components": [c.as_dict() for c in self.components]}


MAX_REGULATORY = 40
MAX_REVIEWS = 25
MAX_DEALS = 15
MAX_FACTS = 10
MAX_KITCHEN = 10

# Recovery rate, points per day since the last sealing. 0.35/day means a venue climbs from the
# floor of 4 back to the full 40 in roughly a hundred days of no further action.
REGULATORY_RECOVERY_PER_DAY = 0.35
REGULATORY_FLOOR = 4

# Above this share of reviews flagged, the review component is worth nothing.
FLAG_RATE_CEILING = 0.30

# Verifications needed for full marks on access facts.
FACT_TARGET = 200


def days_since(then: dt.date, now: dt.date | None = None) -> int:
    now = now or dt.datetime.now(dt.UTC).date()
    return max(0, (now - then).days)


def compute(
    *,
    sealings: list[dict],
    review_flag_rate: float,
    review_count: int,
    deals: list[dict],
    fact_verifications: int,
    fact_keys: int,
    kitchen_transparency: bool,
    now: dt.date | None = None,
) -> TrustScore:
    """The five components, in the order the venue page renders them."""
    components: list[Component] = []

    if sealings:
        latest = sealings[0]
        days = days_since(latest["event_date"], now)
        reg_pts = int(clamp(round(REGULATORY_FLOOR + days * REGULATORY_RECOVERY_PER_DAY),
                            REGULATORY_FLOOR, MAX_REGULATORY))
        note = f"Sealed {days} days ago by {latest['authority']}"
    else:
        reg_pts = MAX_REGULATORY
        note = "No enforcement action on record"
    components.append(Component("Regulatory record", reg_pts, MAX_REGULATORY, note))

    rev_pts = round(clamp(1 - review_flag_rate / FLAG_RATE_CEILING, 0, 1) * MAX_REVIEWS)
    flagged = round(review_flag_rate * review_count)
    components.append(
        Component(
            "Review authenticity", rev_pts, MAX_REVIEWS,
            f"{flagged:,} of {review_count:,} reviews flagged "
            f"({round(review_flag_rate * 100)}%)",
        )
    )

    if deals:
        honoured = sum(d["honoured"] for d in deals) / len(deals)
        deal_pts = round(clamp(honoured, 0, 1) * MAX_DEALS)
        d = deals[0]
        deal_note = (
            f"{d['bank']} {d['claimed']} honoured on "
            f"{round(d['honoured'] * d['n'])} of {d['n']} verified visits"
        )
    else:
        # Not zero. A venue that advertises no bank offer has nothing to break, and scoring
        # it as though it had broken one would punish honesty.
        deal_pts = 12
        deal_note = "No advertised bank offers to verify"
    components.append(Component("Deal truth", deal_pts, MAX_DEALS, deal_note))

    fact_pts = round(clamp(fact_verifications / FACT_TARGET, 0, 1) * MAX_FACTS)
    components.append(
        Component(
            "Fact verification", fact_pts, MAX_FACTS,
            f"{fact_verifications} diner verifications across {fact_keys} access facts",
        )
    )

    kitchen_pts = MAX_KITCHEN if kitchen_transparency else 4
    components.append(
        Component(
            "Kitchen transparency", kitchen_pts, MAX_KITCHEN,
            "Opted in to kitchen transparency" if kitchen_transparency else "Not opted in",
        )
    )

    return TrustScore(total=sum(c.pts for c in components), components=components)


async def compute_for_venue(
    session: AsyncSession, venue_id: uuid.UUID, now: dt.date | None = None
) -> TrustScore:
    """Gather the inputs from the database and score.

    Only PUBLISHED regulatory events count. An unreviewed low-confidence match must never
    move a real venue's score, which is the same 0.90 threshold that keeps it off the page
    (§10.2).
    """
    sealings = [
        dict(r)
        for r in (
            await session.execute(
                text(
                    """
                    SELECT event_date, authority, event_type
                      FROM regulatory_event
                     WHERE venue_id = :v AND published
                       AND event_type IN ('sealed', 'fined')
                       -- A later clearance for the same venue cancels the sealing, and is
                       -- displayed with equal prominence (§14 rule 6).
                       AND NOT EXISTS (
                           SELECT 1 FROM regulatory_event c
                            WHERE c.venue_id = regulatory_event.venue_id
                              AND c.published
                              AND c.event_type IN ('cleared', 'reopened')
                              AND c.event_date >= regulatory_event.event_date
                       )
                     ORDER BY event_date DESC
                    """
                ),
                {"v": venue_id},
            )
        ).mappings().all()
    ]

    reviews = (
        await session.execute(
            text(
                "SELECT count(*) AS n, "
                "count(*) FILTER (WHERE flagged) AS flagged "
                "FROM review_sample WHERE venue_id = :v"
            ),
            {"v": venue_id},
        )
    ).mappings().one()
    review_count = reviews["n"] or 0
    flag_rate = (reviews["flagged"] / review_count) if review_count else 0.0

    facts = (
        await session.execute(
            text(
                "SELECT count(*) AS n, count(DISTINCT fact_key) AS keys "
                "FROM fact_verification WHERE venue_id = :v"
            ),
            {"v": venue_id},
        )
    ).mappings().one()

    attributes = await session.scalar(
        text("SELECT attributes FROM venue WHERE id = :v"), {"v": venue_id}
    )
    kitchen = bool((attributes or {}).get("kitchen_transparency", {}).get("v"))

    return compute(
        sealings=sealings,
        review_flag_rate=flag_rate,
        review_count=review_count,
        deals=[],  # bank deal verification arrives with the partner surface in Phase 7
        fact_verifications=facts["n"] or 0,
        fact_keys=facts["keys"] or len(attributes or {}),
        kitchen_transparency=kitchen,
        now=now,
    )


async def store(session: AsyncSession, venue_id: uuid.UUID, score: TrustScore) -> None:
    """`trust_score` has no user write path; this is the only way a row gets there."""
    import json

    await session.execute(
        text(
            """
            INSERT INTO trust_score (venue_id, score, components, computed_at)
            VALUES (:v, :score, CAST(:components AS jsonb), now())
            ON CONFLICT (venue_id) DO UPDATE SET
                score = EXCLUDED.score,
                components = EXCLUDED.components,
                computed_at = EXCLUDED.computed_at
            """
        ),
        {
            "v": venue_id,
            "score": score.total,
            "components": json.dumps([c.as_dict() for c in score.components]),
        },
    )
