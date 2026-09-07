"""The Ask surface: one Roman-Urdu sentence in, ranked answers out. Plan §9.

This is the endpoint the product is named for. *"Bache ke sath, 2500 tak, koi acha bbq?"*
becomes a structured query, a ranked list, and a sentence per result explaining why.

**The parser runs first and the model runs second, if at all.** `services/intent.py` handles
about 85% of real queries with no model call, which is what makes Phase 8's acceptance
criterion true: pull the API key and this endpoint still works, in full, on templates.

**`understood` is returned, and it is the audit trail.** The response says what the parser
believed each part of the sentence meant, in the user's own words. A wrong result is then
traceable to a misreading rather than to a model nobody can interrogate. §14 rule 8.
"""

from __future__ import annotations

import asyncio

import datetime as dt

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..auth.deps import Ctx
from ..estimator.scoring import WEIGHTS, search_with_relaxation
from ..services import intent as intent_service
from ..services import llm

# How many results get a model-written sentence. The diner sees about six cards
# before scrolling; past that the template is what ships, and nobody notices.
MODEL_EXPLAINED_ROWS = 6

router = APIRouter(prefix="/v1", tags=["ask"])


class AskIn(BaseModel):
    text: str = Field(min_length=1, max_length=280)
    from_lat: float | None = Field(default=None, ge=-90, le=90)
    from_lng: float | None = Field(default=None, ge=-180, le=180)
    from_area: str | None = Field(default=None, max_length=60)
    limit: int = Field(default=8, ge=1, le=20)
    city: str = "Karachi"
    explain: bool = Field(
        default=True,
        description="Ask a model to phrase the reasons. Templates are used either way.",
    )


@router.post("/ask")
async def ask(body: AskIn, ctx: Ctx) -> dict:
    parsed = intent_service.parse(body.text)
    query = intent_service.to_search_query(
        parsed,
        from_lat=body.from_lat,
        from_lng=body.from_lng,
        limit=body.limit,
        city=body.city,
    )

    result = await search_with_relaxation(ctx.session, query)
    results = [r.as_dict() for r in result["results"]]

    # The template is built for every result regardless. A model is only ever asked to say
    # the same thing more fluently, so no failure below can leave a result unexplained.
    #
    # Two things this loop must not be, both learned by switching a key on:
    #
    # Sequential. Awaiting each call in turn made a twenty-result search take twenty round
    # trips end to end — about a hundred seconds. They do not depend on each other, so they
    # go together.
    #
    # Applied to every row. A diner reads the first few cards; paying a model to write a
    # sentence for the seventeenth is spending on something nobody will see. The rest get
    # the template, which is the same sentence with less polish.
    use_model = body.explain and llm.available()
    explained = await asyncio.gather(
        *(
            llm.explain_result(
                row,
                party=parsed.party,
                from_area=body.from_area,
                allow_model=use_model and i < MODEL_EXPLAINED_ROWS,
            )
            for i, row in enumerate(results)
        )
    )
    explained = [
        {**row, "why": sentence, "why_source": source}
        for row, (sentence, source) in zip(results, explained, strict=True)
    ]

    return {
        "query": {
            "raw": parsed.raw,
            # What the system believed you said. The audit trail.
            "understood": [e.as_dict() for e in parsed.extracted],
            "parsed_by": "parser" if parsed.understood else "nothing recognised",
            "party": parsed.party,
            "budget_pkr": parsed.budget_pkr,
            "budget_total_pkr": parsed.budget_total_pkr,
            "max_travel_min": parsed.max_travel_min,
            "mood": parsed.mood,
            "dish": query.dish,
            "diet": parsed.diet,
        },
        "results": explained,
        "count": len(explained),
        "relaxed": result.get("relaxed", False),
        "relaxed_note": result.get("relaxed_note"),
        # Present only when the list is empty: what was most likely in
        # the way, so the caller is never left with a bare [].
        "empty_reason": result.get("empty_reason"),
        "weights": WEIGHTS,
        "live_fraction": round(
            sum(1 for r in result["results"] if r.occupancy_source == "live")
            / max(1, len(result["results"])), 3
        ),
        # Said plainly, because "the AI recommended this" and "a scorer ranked this and a
        # model phrased it" are different claims and only one of them is true here.
        "explanations": {
            "source": "model" if use_model else "template",
            "note": (
                "Ranking is deterministic. A model only phrases the reason, and never "
                "decides the order."
            ),
        },
        "at": dt.datetime.now(dt.UTC),
    }


@router.get("/ask/parse")
async def parse_only(text: str, ctx: Ctx) -> dict:
    """The parser alone, with no search. Useful for the UI's live "we understood" chips, and
    for checking what the deterministic path makes of a sentence without spending a query."""
    return intent_service.parse(text).as_dict()


@router.get("/llm/status")
async def llm_status(ctx: Ctx) -> dict:
    """Whether explanations are coming from a model or a template, and why.

    Public on purpose. A product that claims its ranking is not done by an LLM should be
    willing to show you when it is calling one and when it is not.
    """
    return {
        "key_configured": bool(llm.settings.gemini_api_key),
        "available": llm.available(),
        "budget": llm.budget.as_dict(),
        "cache": llm.cache_stats(),
        "explanations": "model" if llm.available() else "template",
    }
