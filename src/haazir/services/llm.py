"""Gemini, and the templates that make it optional. Plan §9.

*"With the LLM key removed the product still works end to end using template explanations;
the budget guard trips and degrades without an error reaching the user."*

That criterion is the design. **Ranking is never done by a model.** The scorer produces the
order and the model writes the sentence, which means cost scales with query volume rather than
catalogue size, and it means the explanation can always be produced without one. Every path
through this module has a deterministic answer sitting behind it.

Three controls, in the order they bite:

1. **Templates first, always.** `explain()` builds a sentence from the score's own terms. It
   is the default, not the degraded mode; a model is asked only to say the same thing more
   fluently.
2. **A hard monthly ceiling** in config, with a warning at 70%.
3. **At 90%, stop calling out entirely.** The product gets less fluent and never breaks, and
   no error reaches the user.

The spend counter is in-process and resets on deploy, which is the right trade at one
instance: a ceiling that undercounts after a restart is a smaller problem than a database
round trip on every explanation to keep a number that only has to be roughly right.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass, field

import httpx

from ..config import settings

log = logging.getLogger("haazir.llm")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# One model for both jobs, chosen by measurement rather than by version number.
#
# The reasoning models are actively wrong for this. Asked for a one-sentence explanation with
# `maxOutputTokens: 700`, gemini-3.6-flash spent 673 tokens thinking and still returned a
# truncated half-sentence — 804 tokens for no answer. gemini-3.1-flash-lite returned the
# complete sentence, in the right Roman-Urdu register, in 180 tokens total. The work here is
# rewording facts that have already been computed; there is nothing to reason about, and
# paying a thinking budget to discover that is the expensive way to learn it.
#
# The 2.5 family is not an option: the API refuses it for new keys and points at 3.x.
MODEL_INTENT = "gemini-3.1-flash-lite"
MODEL_EXPLAIN = "gemini-3.1-flash-lite"

# Approximate per-million-token prices, used only to keep the running total honest enough to
# trip the guard. Not billing.
PRICE_PER_MTOK = {
    MODEL_INTENT: {"in": 0.10, "out": 0.40},
    MODEL_EXPLAIN: {"in": 0.10, "out": 0.40},
}

WARN_AT = 0.70
DEGRADE_AT = 0.90

_TIMEOUT = httpx.Timeout(8.0, connect=3.0)


@dataclass
class Budget:
    """Running spend for the month. Deliberately in memory; see the module note."""

    spent_usd: float = 0.0
    calls: int = 0
    degraded_since: dt.datetime | None = None
    month: int = field(default_factory=lambda: dt.datetime.now(dt.UTC).month)

    def _roll_month(self) -> None:
        now = dt.datetime.now(dt.UTC).month
        if now != self.month:
            self.month, self.spent_usd, self.calls, self.degraded_since = now, 0.0, 0, None

    @property
    def ceiling(self) -> float:
        return max(0.01, settings.llm_monthly_ceiling_usd)

    @property
    def fraction(self) -> float:
        self._roll_month()
        return self.spent_usd / self.ceiling

    @property
    def degraded(self) -> bool:
        return self.fraction >= DEGRADE_AT

    def record(self, model: str, tokens_in: int, tokens_out: int) -> None:
        self._roll_month()
        price = PRICE_PER_MTOK.get(model, {"in": 3.0, "out": 15.0})
        self.spent_usd += (tokens_in * price["in"] + tokens_out * price["out"]) / 1_000_000
        self.calls += 1

        if self.degraded and self.degraded_since is None:
            self.degraded_since = dt.datetime.now(dt.UTC)
            log.warning(
                "LLM budget at %.0f%% of $%.2f: falling back to template explanations",
                self.fraction * 100, self.ceiling,
            )
        elif self.fraction >= WARN_AT:
            log.info("LLM budget at %.0f%%", self.fraction * 100)

    def as_dict(self) -> dict:
        return {
            "spent_usd": round(self.spent_usd, 4),
            "ceiling_usd": self.ceiling,
            "fraction": round(self.fraction, 4),
            "calls": self.calls,
            "degraded": self.degraded,
            "degraded_since": self.degraded_since,
        }


budget = Budget()


def available() -> bool:
    """Whether a model call is worth attempting at all."""
    return bool(settings.gemini_api_key) and not budget.degraded


# --- the templates, which are the default --------------------------------------


def explain(result: dict, party: int = 1, from_area: str | None = None) -> str:
    """One sentence about a ranked result, built from the score's own terms.

    Port of `explain` in `app/assets/js/app.js`. Nothing here is generated: every number in
    the sentence is one the caller can also see in `factors` and `live`, which is what makes
    the explanation a read-out rather than a claim.
    """
    live = result.get("live") or {}
    parts: list[str] = []

    wait = live.get("wait_p50_min") or 0
    occupancy = live.get("occupancy") or 0
    confidence = live.get("confidence") or 0
    source = live.get("source", "prior")

    if wait < 6:
        parts.append(f"Seated on arrival at {round(occupancy * 100)}% full.")
    else:
        parts.append(f"{round(wait)} min wait, {round(confidence * 100)}% confident.")

    # The provenance travels with the number, in words, because a reader of the sentence
    # should not have to go and look at a field to know whether anyone measured this.
    if source != "live":
        parts.append(
            "That is this venue's usual pattern for the hour, not a live reading."
            if source == "prior"
            else "That is a category estimate; nobody has reported from here yet."
        )

    spend = result.get("expected_spend_pkr")
    if spend:
        who = "person" if party == 1 else "people"
        parts.append(f"Rs {spend:,} for {party} {who}.")

    travel = result.get("travel_min")
    if travel is not None:
        parts.append(
            f"{travel} min from {from_area}." if from_area else f"{travel} min away."
        )

    trust = result.get("trust_score")
    if trust is not None and trust < 55:
        parts.append(f"Trust score {trust}; check the record before you go.")

    return " ".join(parts)


def explain_group(solution: dict) -> str:
    """Why this venue, for a group. The binding member is the whole answer."""
    satisfaction = solution.get("satisfaction") or []
    if not satisfaction:
        return solution.get("venue_name", "This venue") + " fits the group."
    worst = min(satisfaction, key=lambda s: s["u"])
    return (
        f"{solution['venue_name']} works for everyone who answered. "
        f"The tightest fit is {worst['name']} at {round(worst['u'] * 100)}%; nobody scores "
        f"lower. Chosen by maximising the worst-served member, not the average."
    )


# --- the model, when it is worth calling ---------------------------------------

_EXPLAIN_SYSTEM = (
    "You rewrite a restaurant recommendation's own numbers into one natural sentence for a "
    "diner in Karachi.\n"
    "RULES:\n"
    "- Use ONLY the numbers given. Never invent a dish, a price, a wait or a fact.\n"
    # Written as a requirement, not a prohibition. The first version said only "never
    # imply a live measurement", which the model satisfied by staying silent about the
    # source — and a confident occupancy figure with no caveat is exactly the claim this
    # product exists to not make. The template always says it; the model must too.
    "- If the source is NOT 'live', you MUST say the figure is this venue's usual "
    "pattern for this hour and not a live reading. Never omit that.\n"
    "- Round numbers as a person would: '1 min', not '1.0 min'.\n"
    "- One or two short sentences. No greeting, no emoji, no markdown.\n"
    "- Roman Urdu is fine if the user's own words were Roman Urdu."
)


async def _call(model: str, system: str, prompt: str, max_tokens: int = 160) -> str | None:
    """One request. Returns None on any failure; the caller already has a template."""
    if not settings.gemini_api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                GEMINI_URL.format(model=model),
                headers={
                    "x-goog-api-key": settings.gemini_api_key,
                    "content-type": "application/json",
                },
                json={
                    "system_instruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": max_tokens},
                },
            )
        if response.status_code != 200:
            log.warning("Gemini returned %s: %s", response.status_code, response.text[:200])
            return None

        payload = response.json()
        usage = payload.get("usageMetadata", {})
        # `candidatesTokenCount` excludes thinking tokens, which are billed as output. The
        # chosen model does not think, so the two agree; the max keeps the guard honest if a
        # model that does is ever configured here.
        out = max(
            usage.get("candidatesTokenCount", 0),
            usage.get("totalTokenCount", 0) - usage.get("promptTokenCount", 0),
        )
        budget.record(model, usage.get("promptTokenCount", 0), out)

        candidates = payload.get("candidates") or []
        if not candidates:
            # A safety block or a filtered response arrives this way, with no error status.
            log.warning("Gemini returned no candidate: %s", str(payload)[:200])
            return None
        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if "text" in p)
        return text.strip() or None
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.warning("Gemini unreachable, using the template: %s", exc)
        return None


# --- intent, when the deterministic passes found nothing ----------------------

_INTENT_SYSTEM = (
    "You extract search filters from what a diner in Karachi typed. Reply with JSON only.\n"
    'Shape: {"area": string|null, "cuisine": string|null, "dish": string|null}\n'
    "RULES:\n"
    "- `area` must be one of the AREAS listed in the user message, copied exactly, or null.\n"
    "- `cuisine` must be one of the CUISINES listed, copied exactly, or null.\n"
    "- `dish` is a specific dish if one is named, else null.\n"
    "- Roman Urdu, Urdu script and English all appear. 'sasta' is not a cuisine.\n"
    "- Null is a good answer. Never guess an area from a venue name you happen to know."
)


async def extract_intent(
    phrase: str, areas: list[str], cuisines: list[str]
) -> dict[str, str | None]:
    """`{area, cuisine, dish}` for a sentence the regex and fuzzy passes could not read.

    The closed lists go in the prompt and the answer is checked against them on the way out,
    so a hallucinated neighbourhood cannot become a filter. This is the only place a model
    touches the search, and it runs only when the deterministic passes have already failed —
    the cost is a handful of calls a day, not one per query.
    """
    empty: dict[str, str | None] = {"area": None, "cuisine": None, "dish": None}
    if not available():
        return empty

    prompt = (
        f"AREAS: {', '.join(areas)}\n"
        f"CUISINES: {', '.join(cuisines)}\n\n"
        f"Diner typed: {phrase}"
    )
    raw = await _call(MODEL_INTENT, _INTENT_SYSTEM, prompt, max_tokens=120)
    if not raw:
        return empty

    try:
        # Models like to wrap JSON in a fence however firmly you ask them not to.
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        parsed = json.loads(cleaned.strip())
    except (ValueError, AttributeError):
        log.warning("intent extraction was not JSON: %s", raw[:120])
        return empty

    area = parsed.get("area")
    cuisine = parsed.get("cuisine")
    return {
        # Checked against the closed lists, case-insensitively. Anything invented is dropped.
        "area": next((a for a in areas if isinstance(area, str) and a.lower() == area.lower()), None),
        "cuisine": next(
            (c for c in cuisines if isinstance(cuisine, str) and c.lower() == cuisine.lower()), None
        ),
        "dish": parsed.get("dish") if isinstance(parsed.get("dish"), str) else None,
    }


# --- semantic cache -----------------------------------------------------------

# "Late night biryani in Gulshan" arrives a hundred times a day; generate it once. Keyed on
# the shape of the answer rather than the wording of the question, so two phrasings that
# produce the same ranking share a sentence.
_cache: dict[str, str] = {}
CACHE_MAX = 2_000


def _cache_key(venue_id: str, band: str, party: int, hour_bucket: int) -> str:
    raw = f"{venue_id}|{band}|{party}|{hour_bucket}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


async def explain_result(
    result: dict, party: int = 1, from_area: str | None = None, *, allow_model: bool = True
) -> tuple[str, str]:
    """`(sentence, source)` where source is `template`, `model` or `cache`.

    The template is computed first and unconditionally. A model is only ever asked to improve
    on a sentence that already exists, so no failure path can leave the caller without one.
    """
    template = explain(result, party, from_area)

    if not allow_model or not available():
        return template, "template"

    live = result.get("live") or {}
    key = _cache_key(
        str(result.get("venue_id")), live.get("band", "?"), party,
        dt.datetime.now(dt.UTC).hour // 3,
    )
    if key in _cache:
        return _cache[key], "cache"

    prompt = (
        f"Venue: {result.get('name')}\n"
        f"Area: {result.get('area')}\n"
        f"Occupancy: {round((live.get('occupancy') or 0) * 100)}% ({live.get('band')})\n"
        f"Source of that figure: {live.get('source')}\n"
        f"Confidence: {round((live.get('confidence') or 0) * 100)}%\n"
        f"Wait: {live.get('wait_p50_min')} min\n"
        f"Spend for {party}: Rs {result.get('expected_spend_pkr')}\n"
        f"Travel: {result.get('travel_min')} min\n"
        f"Score terms: {result.get('factors')}\n\n"
        f"Write the one-sentence reason this was recommended."
    )
    sentence = await _call(MODEL_EXPLAIN, _EXPLAIN_SYSTEM, prompt)
    if sentence is None:
        return template, "template"

    if len(_cache) >= CACHE_MAX:
        _cache.clear()
    _cache[key] = sentence
    return sentence, "model"


def cache_stats() -> dict:
    return {"entries": len(_cache), "max": CACHE_MAX}


def reset() -> None:
    """Tests only."""
    global budget
    _cache.clear()
    budget = Budget()
