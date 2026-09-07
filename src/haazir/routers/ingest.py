"""Admin ingestion. Plan §7, §10.1.

The scraper writes JSONL to disk and `scripts/ingest.py` loads it. This endpoint is the same
loader over HTTP, for the case where the scrape runs somewhere the API cannot read the
filesystem of.

It is admin-only and it is synchronous. A background job would be tidier for a large batch,
but the honest report is the point of running an ingest at all: how many were rejected and
why, how many priors came from Google rather than a guess. Returning that report to the
caller who asked for the load, rather than filing it in a log, is what makes those numbers
get read.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..auth.deps import Ctx, CurrentAdmin
from ..db import service_session
from ..services import ingest_sfa
from ..services import ingest_venues as ingest

router = APIRouter(prefix="/v1/admin/ingest", tags=["admin"])

MAX_BATCH = 5_000


class IngestIn(BaseModel):
    venues: list[dict] = Field(default_factory=list, max_length=MAX_BATCH)
    menu_items: list[dict] = Field(default_factory=list, max_length=MAX_BATCH * 8)
    reviews: list[dict] = Field(default_factory=list, max_length=MAX_BATCH * 8)
    city: str = "Karachi"


@router.post("/venues", status_code=status.HTTP_200_OK)
async def ingest_venues(body: IngestIn, principal: CurrentAdmin, ctx: Ctx) -> dict:
    if not body.venues and not body.menu_items and not body.reviews:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="nothing to ingest"
        )

    # A separate service-scoped session, not the request's. Ingestion writes venues under
    # `app.service`, and the request context deliberately carries the caller's claims.
    async with service_session() as session:
        report = ingest.IngestReport()
        if body.venues:
            report = await ingest.load_venues(session, body.venues, city_name=body.city)
        if body.menu_items:
            await ingest.load_menu_items(session, body.menu_items, report)
        if body.reviews:
            await ingest.load_reviews(session, body.reviews, report)

    return report.as_dict()


# --- Sindh Food Authority. Plan §10.2 -----------------------------------------


class SfaRecordIn(BaseModel):
    venue_name: str = Field(min_length=3, max_length=160)
    event_type: str = Field(pattern="^(sealed|fined|notice|cleared|reopened)$")
    event_date: dt.date
    source_url: str = Field(min_length=8, max_length=600)
    source_name: str = Field(max_length=80)
    area: str | None = Field(default=None, max_length=60)
    reason: str | None = Field(default=None, max_length=600)
    fine_pkr: int | None = Field(default=None, ge=0, le=100_000_000)
    authority: str = "Sindh Food Authority"


class SfaIngestIn(BaseModel):
    records: list[SfaRecordIn] = Field(min_length=1, max_length=500)
    city: str = "Karachi"


@router.post("/sfa", status_code=status.HTTP_200_OK)
async def ingest_sfa_records(
    body: SfaIngestIn, principal: CurrentAdmin, ctx: Ctx
) -> dict:
    """Match enforcement records to venues and file them.

    Anything under 0.90 lands unpublished and invisible to diners. The response returns every
    match with its score and the reason for it, because an operator approving the last mile
    needs to see why the machine thought what it thought.
    """
    records = [
        ingest_sfa.SfaRecord(
            venue_name=r.venue_name, event_type=r.event_type, event_date=r.event_date,
            source_url=r.source_url, source_name=r.source_name, area=r.area,
            reason=r.reason, fine_pkr=r.fine_pkr, authority=r.authority,
        )
        for r in body.records
    ]

    async with service_session() as session:
        report, matches = await ingest_sfa.ingest(session, records, body.city)

    return {
        **report.as_dict(),
        "matches": [
            {
                "raw_name": m.record.venue_name,
                "matched_venue": m.venue_name,
                "venue_id": str(m.venue_id) if m.venue_id else None,
                "confidence": m.confidence,
                "name_score": m.name_score,
                "place_score": m.place_score,
                "published": m.published,
                "why": m.reason,
            }
            for m in matches
        ],
    }


@router.get("/review-queue")
async def review_queue(principal: CurrentAdmin, ctx: Ctx, limit: int = 50) -> dict:
    """Records a human has to decide on. Ordered by confidence, so the near-misses that are
    most likely correct come first and the obvious rubbish sinks."""
    async with service_session() as session:
        rows = await ingest_sfa.review_queue(session, limit)
    return {
        "count": len(rows),
        "items": rows,
        "note": (
            "None of these is visible to a diner. Publishing one is a public statement about "
            "a named business; check the source before you do."
        ),
    }


class ReviewDecisionIn(BaseModel):
    publish: bool
    venue_id: uuid.UUID | None = Field(
        default=None, description="Correct the match before publishing, if it was wrong."
    )


@router.post("/review/{event_id}/decide")
async def decide_review(
    event_id: uuid.UUID, body: ReviewDecisionIn, principal: CurrentAdmin, ctx: Ctx
) -> dict:
    async with service_session() as session:
        result = await ingest_sfa.decide(
            session, event_id, publish=body.publish,
            reviewer_id=principal.subject, venue_id=body.venue_id,
        )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such record")
    return {**result, "id": str(result["id"]),
            "venue_id": str(result["venue_id"]) if result["venue_id"] else None}
