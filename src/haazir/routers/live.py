"""The live feed. Plan §7, §8.

`GET /venues/{id}/live/stream` is the demo's moment: a staff tablet taps "full" in one window
and the diner's screen moves in another, with the confidence bar and the source weights
visibly changing. Everything else in this product is a claim about accuracy; this is the one
the audience can watch happen.

Server-sent events rather than WebSockets because the traffic is one-directional and SSE
reconnects itself. A browser that loses the connection retries automatically and sends
`Last-Event-ID`, which is what makes a phone coming out of a tunnel resume rather than
re-render or miss the change it was waiting for.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from ..auth.deps import Ctx
from ..services import lookup, realtime

router = APIRouter(prefix="/v1/venues", tags=["live"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # nginx and most Pakistani ISP proxies buffer responses by default, which holds every
    # frame until the connection closes and makes a live stream look broken.
    "X-Accel-Buffering": "no",
}


@router.get("/{ident}/live/stream")
async def live_stream(
    ident: str,
    request: Request,
    ctx: Ctx,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Open a live feed for one venue. Public: browsing is never gated (§5).

    Slug or UUID, matching the card and `/live`: a page that routes by slug subscribes with
    the same string it was given.
    """
    venue_id = await lookup.venue_id_for(ctx.session, ident)
    if not venue_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such venue")

    since = realtime.parse_last_event_id(last_event_id)

    async def frames():
        # The session and its claims belong to the request, and this generator outlives it.
        # Nothing here touches the database again; the stream carries only what `publish`
        # was given.
        async for frame in realtime.event_stream(venue_id, since):
            if await request.is_disconnected():
                break
            yield frame

    return StreamingResponse(frames(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/{venue_id}/trust")
async def venue_trust(venue_id: uuid.UUID, ctx: Ctx) -> dict:
    """The trust score, its components, the regulatory record, and the venue's replies.

    Three rules from §14 rule 6 are visible in the shape of this response. Only published
    records appear, so an unreviewed low-confidence match never reaches a diner. Every record
    carries its `source_url`, because a hygiene claim about a named business without a
    citation is an accusation. And a `cleared` or `reopened` event is returned in the same
    list as the sealing that preceded it, not tucked away.
    """
    row = (
        await ctx.session.execute(
            text(
                """
                SELECT v.id, v.name, t.score, t.components, t.computed_at
                  FROM venue v
                  LEFT JOIN trust_score t ON t.venue_id = v.id
                 WHERE v.id = :v
                """
            ),
            {"v": venue_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such venue")

    events = (
        await ctx.session.execute(
            text(
                """
                SELECT e.id, e.event_type, e.event_date, e.authority, e.reason, e.fine_pkr,
                       e.source_url, e.source_name, e.match_confidence,
                       COALESCE(
                           jsonb_agg(
                               jsonb_build_object('body', r.body, 'created_at', r.created_at)
                               ORDER BY r.created_at
                           ) FILTER (WHERE r.id IS NOT NULL),
                           '[]'::jsonb
                       ) AS replies
                  FROM regulatory_event e
                  LEFT JOIN regulatory_reply r ON r.event_id = e.id AND r.published
                 WHERE e.venue_id = :v
                 GROUP BY e.id
                 ORDER BY e.event_date DESC
                """
            ),
            {"v": venue_id},
        )
    ).mappings().all()

    return {
        "venue_id": str(venue_id),
        "score": row["score"],
        "components": row["components"] or [],
        "computed_at": row["computed_at"],
        "scored": row["score"] is not None,
        # RLS already filters unpublished records; saying so here means a reader of the
        # response does not have to know that to trust it.
        "regulatory": [dict(e) for e in events],
        "regulatory_note": (
            "Only records matched to this venue with at least 0.90 confidence and reviewed "
            "for publication appear here. Every one carries its source."
        ),
        "at": dt.datetime.now(dt.UTC),
    }
