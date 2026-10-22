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

import asyncio
import datetime as dt
import hashlib
import json
import logging
import time
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
# The intent fallback sits in front of a search the diner is waiting on, so it gets less
# patience than an explanation that fills in after the results are already on screen.
_INTENT_TIMEOUT = httpx.Timeout(4.5, connect=2.0)


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


async def _call(
    model: str, system: str, prompt: str, max_tokens: int = 160,
    timeout: httpx.Timeout | None = None,
) -> str | None:
    """One request through the model chain, `model` tried first. None on any failure; the
    caller already has a template."""
    seconds = (timeout.read if isinstance(timeout, httpx.Timeout) and timeout.read
               else _TIMEOUT.read)
    payload = await call_raw(
        {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        },
        model=model, timeout=float(seconds),
    )
    return text_of(payload).strip() or None


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
    raw = await _call(MODEL_INTENT, _INTENT_SYSTEM, prompt, max_tokens=120,
                      timeout=_INTENT_TIMEOUT)
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


# --- the answer above the list ----------------------------------------------

_ANSWER_SYSTEM = (
    "You are HAAZIR, a live restaurant guide for Karachi. Answer what the diner asked in one "
    "or two short sentences: recommend the first venue and, if it helps, one alternative.\n"
    "RULES:\n"
    "- Use ONLY the venues and numbers given. Never invent a dish, a price, a wait or a fact.\n"
    "- If a venue's source is NOT 'live', say its figure is the usual pattern for this hour, "
    "not a live reading.\n"
    "- Reply in the diner's own language: Roman Urdu if they wrote Roman Urdu, else English.\n"
    "- Round numbers as a person would. No greeting, no emoji, no markdown."
)


def answer_template(rows: list[dict]) -> str:
    """The same answer from the numbers alone, for when no model is available."""
    if not rows:
        return ""
    top = rows[0]
    live = top.get("live") or {}
    text_ = f"Top pick: {top.get('name')} in {top.get('area')}, about {top.get('travel_min')} min away"
    if live.get("wait_p50_min") is not None:
        text_ += f", around {round(float(live['wait_p50_min']))} min wait"
    text_ += "."
    if live.get("source") != "live":
        text_ += " That is its usual pattern for this hour, not a live reading."
    if len(rows) > 1:
        text_ += f" Also worth a look: {rows[1].get('name')}."
    return text_


async def answer(query: str | None, rows: list[dict], party: int = 1) -> tuple[str, str]:
    """`(text, source)`: one recommendation in the diner's words, written from the ranked rows.

    The ranking is already decided; the model only says it. The template is built first, so a
    missing key, a timeout or a tripped budget still returns an answer.
    """
    template = answer_template(rows)
    if not rows or not available():
        return template, "template"

    key = "answer|" + _cache_key(
        (query or "").strip().lower() + "|" + ",".join(str(r.get("venue_id")) for r in rows[:3]),
        "", party, dt.datetime.now(dt.UTC).hour // 3,
    )
    if key in _cache:
        return _cache[key], "cache"

    lines = []
    for i, r in enumerate(rows[:3], start=1):
        live = r.get("live") or {}
        lines.append(
            f"{i}. {r.get('name')} ({r.get('area')}): "
            f"{round((live.get('occupancy') or 0) * 100)}% full, band {live.get('band')}, "
            f"source {live.get('source')}, wait {live.get('wait_p50_min')} min, "
            f"travel {r.get('travel_min')} min"
        )
    prompt = (
        f"Diner typed: {query or '(nothing, just browsing)'}\n"
        f"Party size: {party}\n"
        "Ranked venues, best first:\n"
        + "\n".join(lines)
        + "\n\nWrite the answer."
    )
    text_ = await _call(MODEL_EXPLAIN, _ANSWER_SYSTEM, prompt, max_tokens=140)
    if text_ is None:
        return template, "template"
    if len(_cache) >= CACHE_MAX:
        _cache.clear()
    _cache[key] = text_
    return text_, "model"


# --- raw calls, through a chain of models ------------------------------------

# Any one Gemini model is regularly "experiencing high demand" (503) or simply slow for
# minutes at a time; on 22 Oct three of the five models this key can use were failing while
# the other two answered in a second and a half. So every call walks a chain, gives each
# model a short leash, and benches a failing one for a minute so the next call goes straight
# to one that works. The chain is env-configurable (GEMINI_MODELS, comma separated, names as
# ListModels prints them); GEMINI_CHAT_MODEL, if set, goes first.
DEFAULT_CHAIN = (
    "gemini-3.5-flash,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite,"
    "gemini-flash-latest"
)
MODEL_CHAIN: list[str] = list(dict.fromkeys(
    m.strip()
    for m in [settings.gemini_chat_model, *(settings.gemini_models or DEFAULT_CHAIN).split(",")]
    if m and m.strip()
))
MODEL_CHAT = MODEL_CHAIN[0]
for _m in MODEL_CHAIN:
    PRICE_PER_MTOK.setdefault(_m, {"in": 0.30, "out": 2.50})

ATTEMPT_TIMEOUT_S = 5.0
BENCH_S = 60.0
_benched: dict[str, float] = {}


def _order(preferred: str | None) -> list[str]:
    chain = list(dict.fromkeys([m for m in [preferred, *MODEL_CHAIN] if m]))
    now = time.monotonic()
    ready = [m for m in chain if _benched.get(m, 0) <= now]
    # If everything is benched, try them all anyway: a benched model beats no answer.
    return ready or chain


def _thinking(model: str) -> dict:
    # The 3.1 and 2.5 models take a budget of zero; 3.5 and later refuse it and take a level.
    return ({"thinkingBudget": 0} if model.startswith(("gemini-3.1", "gemini-2.5"))
            else {"thinkingLevel": "low"})


def _without_signatures(body: dict) -> dict:
    """Thought signatures are bound to the model that wrote them; another model refuses a
    conversation carrying them."""
    return {**body, "contents": [
        {**c, "parts": [{k: v for k, v in p.items() if k != "thoughtSignature"}
                        for p in c.get("parts", [])]}
        for c in body.get("contents", [])
    ]}


async def call_raw(
    body: dict, *, model: str | None = None, timeout: float = 8.0
) -> dict | None:
    """One `generateContent` with a caller-built body, through the model chain.

    `model` is tried first (the chat passes the model that produced the previous turn, whose
    thought signatures are in the body). The parsed response carries `_model`, the model that
    answered, or None when every model failed inside `timeout` plus a little grace. Thinking is
    switched off unless the caller asks. Spend is recorded against the monthly guard.
    """
    if not available():
        return None
    deadline = time.monotonic() + timeout + 4.0
    for attempt, m in enumerate(_order(model)):
        remaining = deadline - time.monotonic()
        if remaining < 1.0:
            break
        if attempt:
            await asyncio.sleep(min(0.3 * attempt, 1.0))
        sent = body if (model and m == model) else _without_signatures(body)
        sent = {**sent, "generationConfig": dict(sent.get("generationConfig") or {})}
        sent["generationConfig"].setdefault("thinkingConfig", _thinking(m))
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(min(ATTEMPT_TIMEOUT_S, remaining), connect=3.0)
            ) as client:
                response = await client.post(
                    GEMINI_URL.format(model=m),
                    headers={"x-goog-api-key": settings.gemini_api_key,
                             "content-type": "application/json"},
                    json=sent,
                )
        except httpx.HTTPError as exc:
            log.info("Gemini %s unreachable (%s); trying the next model", m, type(exc).__name__)
            _benched[m] = time.monotonic() + BENCH_S
            continue
        if response.status_code != 200:
            log.info("Gemini %s returned %s; trying the next model", m, response.status_code)
            if response.status_code in (429, 500, 503, 504, 404):
                _benched[m] = time.monotonic() + BENCH_S
            continue
        try:
            payload = response.json()
        except ValueError:
            continue
        usage = payload.get("usageMetadata", {})
        budget.record(
            m, usage.get("promptTokenCount", 0),
            max(usage.get("candidatesTokenCount", 0),
                usage.get("totalTokenCount", 0) - usage.get("promptTokenCount", 0)),
        )
        if not payload.get("candidates"):
            # A safety block arrives this way; another model will not judge it differently.
            log.warning("Gemini returned no candidate: %s", str(payload)[:200])
            return None
        payload["_model"] = m
        return payload
    log.warning("every Gemini model failed; the template answers")
    return None


def text_of(payload: dict | None) -> str:
    if not payload:
        return ""
    parts = payload["candidates"][0].get("content", {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))


async def generate_json(system: str, prompt: str, *, max_tokens: int = 4096,
                        timeout: float = 20.0) -> dict | list | None:
    """JSON mode. None on any failure, including output that does not parse."""
    payload = await call_raw(
        {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens,
                                 "responseMimeType": "application/json"},
        },
        timeout=timeout,
    )
    raw = text_of(payload).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        log.warning("Gemini JSON did not parse: %s", raw[:200])
        return None


def cache_stats() -> dict:
    return {"entries": len(_cache), "max": CACHE_MAX}


def reset() -> None:
    """Tests only."""
    global budget
    _cache.clear()
    budget = Budget()
