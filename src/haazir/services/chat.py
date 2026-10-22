"""The chat agent. Plan §4 B6.

Gemini with function calling over code that already exists: the scorer, the venue card, the
live state, the forecast, the dish price comparison and the card deals. The model decides
which to call and words the answer; it never ranks and it never names a venue a tool did not
return. Every venue in the response's `venues` list came out of a tool call in this request.

**The deterministic search runs first, before any model.** The last message goes through the
same parser and scorer as `/v1/search`, and the top results are handed to the model as
context. For the common question ("Bahadurabad mein abhi khali biryani, 4 log") that is the
whole answer and the model needs one call, not two, which matters when each call is five
seconds. The same results are the template answer when there is no model at all.

**Search grounding is a second, optional pass.** It is for facts we do not hold (a general
food question), it is only tried when the tools found nothing, and a refusal (this key has no
grounding quota today) costs nothing but the attempt.
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable

from sqlalchemy import text

from ..auth.deps import AuthContext
from . import deals as deals_service
from . import intent as intent_service
from . import llm, lookup
from .clock import local_now

log = logging.getLogger("haazir.chat")

# The whole model conversation, tool rounds included. Past this the template answers.
TOTAL_BUDGET_S = 16.0
PER_CALL_S = 8.0
MAX_TOOL_ROUNDS = 3
CONTEXT_RESULTS = 6
CARD_LIMIT = 5

SYSTEM = (
    "You are HAAZIR, a food assistant for Karachi. You help people decide where to eat right "
    "now, using live table availability, forecasts, prices and card deals.\n"
    "RULES:\n"
    "- Reply in the user's language: Roman Urdu if they wrote Roman Urdu, otherwise English. "
    "Be concise: two to four short sentences.\n"
    "- NEVER invent a restaurant. Only mention venues returned by a tool or listed in the "
    "CONTEXT block. If nothing fits, say so and suggest how to widen the search.\n"
    "- Prices are in PKR (write 'Rs 1,200').\n"
    "- Say 'abhi khali' / 'free right now' only for venues whose status is free and measured "
    "now; for others say it is usually quiet or busy at this hour.\n"
    "- Never mention internal terms: no 'prior', 'confidence', 'sigma', 'estimator', "
    "'source weights', 'model', 'tool' or 'simulated'.\n"
    "- When a card deal applies to the user's bank, mention it with its discount.\n"
    "- For any question about a later time ('9 baje', 'tonight', 'kal'), call get_forecast "
    "and answer from it; never guess how busy a place will be.\n"
    "- Say how busy a place is ONLY with its status phrase, as given (e.g. Abhi khali, Thoda "
    "rush, Full ~20 min wait). Never give a percentage, a number of tables, or the "
    "words moderate, occupancy, band or estimate.\n"
    "- No markdown headings, no emoji, no greeting.\n"
    "- Follow the conversation. 'yahan', 'wahan', 'is restaurant', 'here', 'there' mean the "
    "venue in FOCUS when one is given. For menu or price questions about a venue, call "
    "get_menu and quote dishes with price and portion.\n"
    "- Never say '0 min wait'; say there is no wait right now."
)

TOOLS = [
    {
        "functionDeclarations": [
            {
                "name": "search_venues",
                "description": "Rank Karachi restaurants for a request. Use for any 'where should "
                               "I eat' question.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "dish, cuisine or mood words"},
                        "area": {"type": "string", "description": "Karachi area, e.g. Bahadurabad"},
                        "cuisine": {"type": "string"},
                        "dish": {"type": "string"},
                        "budget_pkr": {"type": "integer", "description": "per person"},
                        "party": {"type": "integer"},
                        "open_now": {"type": "boolean"},
                        "free_now": {"type": "boolean", "description": "only places with free tables now"},
                        "card_bank": {"type": "string", "description": "bank whose card deal to prefer"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "get_venue",
                "description": "Details of one venue: address, phone, cuisines, price, deals.",
                "parameters": {"type": "object", "properties": {"slug": {"type": "string"}},
                               "required": ["slug"]},
            },
            {
                "name": "get_live",
                "description": "How full a venue is right now and the wait.",
                "parameters": {"type": "object", "properties": {"slug": {"type": "string"}},
                               "required": ["slug"]},
            },
            {
                "name": "get_forecast",
                "description": "How busy a venue will be over the next hours, or at a time.",
                "parameters": {
                    "type": "object",
                    "properties": {"slug": {"type": "string"},
                                   "at": {"type": "string", "description": "HH:MM, Karachi time"}},
                    "required": ["slug"],
                },
            },
            {
                "name": "get_menu",
                "description": "A venue's menu: dishes with price and portion (serving size).",
                "parameters": {"type": "object", "properties": {"slug": {"type": "string"}},
                               "required": ["slug"]},
            },
            {
                "name": "compare_dish_prices",
                "description": "Compare one dish's price across restaurants.",
                "parameters": {"type": "object", "properties": {"dish": {"type": "string"}},
                               "required": ["dish"]},
            },
            {
                "name": "get_deals",
                "description": "Bank card deals at restaurants.",
                "parameters": {"type": "object",
                               "properties": {"bank": {"type": "string"},
                                              "area": {"type": "string"}}},
            },
        ]
    }
]

_ROMAN_URDU = {
    "mein", "main", "hai", "hain", "kya", "abhi", "khana", "log", "tak", "koi", "acha", "achi",
    "kahan", "chahiye", "batao", "kaun", "sasta", "wala", "wali", "ke", "ki", "ka", "aur",
    "jagah", "khali", "kitna", "kitne", "bhai", "yaar", "raat", "dopahar",
    "yahan", "wahan", "btao", "bta", "theek", "rahega", "jana", "hoga", "kab", "kaisa",
}


def is_roman_urdu(text: str) -> bool:
    words = set(re.findall(r"[a-z]+", text.lower()))
    return len(words & _ROMAN_URDU) >= 2


def banks_in(text: str) -> list[str]:
    lowered = f" {text.lower()} "
    found = []
    for alias, bank in deals_service.BANK_ALIASES.items():
        if len(alias) > 2 and f" {alias} " in lowered and bank not in found:
            found.append(bank)
    return found


# The only words a customer sees for how busy a place is (FINALPLAN §0 rule 8): never a
# percentage, never "moderate". Roman Urdu, English.
PLAIN = {
    "free": ("Abhi khali", "free right now"),
    "moderate": ("Thoda rush", "a little busy"),
    "busy": ("Rush hai", "busy"),
    "full": ("Full", "full"),
}


# The language of the turn being answered, so every phrase handed to the model is already in
# the user's words; given both, the model pasted "thoda rush / a little busy" verbatim.
_ROMAN: contextvars.ContextVar[bool] = contextvars.ContextVar("chat_roman", default=True)


def status_phrase(band: str | None, wait_min: float | int | None = None,
                  roman: bool | None = None) -> str | None:
    if band not in PLAIN:
        return None
    phrase = PLAIN[band][0 if (_ROMAN.get() if roman is None else roman) else 1]
    wait = round(wait_min or 0)
    if band in ("busy", "full") and wait > 2:
        phrase += f", ~{wait} min wait"
    return phrase


def compact(row: dict, deals: list[dict] | None = None, roman: bool | None = None) -> dict:
    """A search row as the model sees it. Plain words and numbers, nothing internal."""
    live = row.get("live") or {}
    return {
        "slug": row["slug"],
        "name": row["name"],
        "area": row.get("area"),
        "status": status_phrase(live.get("band"), live.get("wait_p50_min"), roman),
        "measured_now": live.get("source") == "live",
        "wait_min": round(live.get("wait_p50_min") or 0),
        "travel_min": row.get("travel_min"),
        "spend_pkr_for_party": row.get("expected_spend_pkr"),
        "cuisines": (row.get("cuisines") or [])[:4],
        "deals": [f"{d['bank']} {d['discount_pct']}% off" for d in (deals or [])[:3]],
    }


class Agent:
    """One chat turn. Holds the request's session and remembers every venue a tool returned."""

    def __init__(self, ctx: AuthContext, *, lat: float | None, lng: float | None,
                 area: str | None, cards: list[str]) -> None:
        self.ctx = ctx
        self.lat, self.lng, self.area = lat, lng, area
        self.cards = cards
        self.seen: dict[str, dict] = {}  # slug -> compact row, in the order first returned
        self.dishes: list[dict] = []
        self.menus: list[dict] = []
        self.tool_calls: list[str] = []

    # --- tools -----------------------------------------------------------

    async def search_venues(self, query: str = "", area: str | None = None,
                            cuisine: str | None = None, dish: str | None = None,
                            budget_pkr: int | None = None, party: int | None = None,
                            open_now: bool = False, free_now: bool = False,
                            card_bank: str | None = None, **_) -> list[dict]:
        from ..routers.search import SearchIn, _ranked

        area = area or self.area
        text = " ".join(x for x in [query, dish, cuisine, f"in {area}" if area else ""] if x)
        parsed = intent_service.parse(text)
        body = SearchIn(
            text=text[:280], from_lat=self.lat, from_lng=self.lng,
            party=max(1, min(30, party or parsed.party)),
            budget=budget_pkr if budget_pkr and budget_pkr >= 50 else parsed.budget_pkr,
            diet=parsed.diet, needs_family=parsed.needs_family, limit=20,
        )
        _q, _result, rows = await _ranked(body, self.ctx)
        if free_now:
            rows = [r for r in rows if (r.get("live") or {}).get("band") in ("free", "moderate")]
        by_venue = await deals_service.for_venues(
            self.ctx.session, [r["venue_id"] for r in rows[:20]]
        )
        bank = deals_service.canonical_bank(card_bank) if card_bank else None
        banks = [bank] if bank else self.cards
        if banks:
            # Venues with a deal on the diner's card go first, the scorer's order kept within.
            rows.sort(key=lambda r: not any(
                d["bank"] in banks for d in by_venue.get(_uuid(r["venue_id"]), [])))
        out = [compact(r, by_venue.get(_uuid(r["venue_id"]))) for r in rows[:CONTEXT_RESULTS]]
        for row in out:
            self.seen.setdefault(row["slug"], row)
        return out

    async def get_venue(self, slug: str, **_) -> dict:
        from ..routers.venues import venue_card

        card = await venue_card(slug, self.ctx)
        self.seen.setdefault(card.slug, {"slug": card.slug, "name": card.name})
        return {
            "slug": card.slug, "name": card.name,
            "area": card.area.name if card.area else None, "address": card.address_full,
            "phone": card.phone, "cuisines": card.cuisines[:6],
            "avg_ticket_pkr": card.avg_ticket_pkr, "rating": card.google_rating,
            "status": (status_phrase(card.live.band, card.live.wait_p50_min)
                       if card.live else None),
            "deals": [f"{d.bank} {d.discount_pct}% off" + (f" up to Rs {d.cap:,}" if d.cap else "")
                      for d in card.deals],
        }

    async def get_live(self, slug: str, **_) -> dict:
        from ..routers.venues import venue_card

        card = await venue_card(slug, self.ctx)
        self.seen.setdefault(card.slug, {"slug": card.slug, "name": card.name})
        live = card.live
        if live is None:
            return {"slug": slug, "status": "unknown"}
        return {"slug": slug, "name": card.name,
                "status": status_phrase(live.band, live.wait_p50_min),
                "wait_min": round(live.wait_p50_min or 0),
                "measured_now": live.source == "live"}

    async def get_forecast(self, slug: str, at: str | None = None, **_) -> dict:
        from ..estimator import forecast
        from ..routers.forecast import forecast_for

        venue_id = await lookup.venue_id_for(self.ctx.session, slug)
        if venue_id is None:
            return {"error": "no such venue"}
        out = await forecast_for(self.ctx.session, venue_id, hours=8)
        points = [{"at": p["at"][11:16], "status": status_phrase(p["band"], p["wait_min"]),
                   "wait_min": p["wait_min"]}
                  for p in out["points"]]
        if at and re.fullmatch(r"\d{1,2}:\d{2}", at):
            h, m = (int(x) for x in at.split(":"))
            now = local_now()
            when = now.replace(hour=h % 24, minute=m % 60, second=0, microsecond=0)
            if when < now:
                when += dt.timedelta(days=1)
            point = forecast.at_time(out, when)
            return {"slug": slug, "at": at,
                    "status": status_phrase(point["band"], point["wait_min"]) if point else None,
                    "wait_min": point["wait_min"] if point else None}
        return {"slug": slug, "now": status_phrase(out["now"]["band"]), "next_hours": points}

    async def get_menu(self, slug: str, **_) -> dict:
        from ..routers.menu import menu_for

        venue_id = await lookup.venue_id_for(self.ctx.session, slug)
        if venue_id is None:
            return {"error": "no such venue"}
        menu = await menu_for(self.ctx.session, venue_id, limit=60)
        priced = [m for m in menu if m["price_pkr"]] or menu
        items = [{"name": m["menu_name"], "section": m["section"], "price_pkr": m["price_pkr"],
                  "portion": m["portion"], "sold_out": m["sold_out"]} for m in priced[:30]]
        self.dishes = [{"slug": slug, **i} for i in items]
        brief = await self.ctx.session.execute(
            text("SELECT v.id, v.slug, v.name, a.name AS area FROM venue v "
                 "LEFT JOIN area a ON a.id = v.area_id WHERE v.id = :v"), {"v": venue_id})
        v = brief.mappings().one()
        self.menus.append({
            "venue": {"id": str(v["id"]), "slug": v["slug"], "name": v["name"],
                      "area": v["area"]},
            "items": [{"dish_id": m["dish_id"], "name": m["menu_name"], "portion": m["portion"],
                       "price_pkr": m["price_pkr"], "section": m["section"]}
                      for m in priced[:30] if m["price_pkr"]],
        })
        return {"slug": slug, "items": items, "count": len(menu),
                "note": None if menu else "No menu has been published for this venue yet."}

    async def compare_dish_prices(self, dish: str, **_) -> dict:
        from ..routers.dishes import dish_prices

        result = await dish_prices(dish, self.ctx, protein=None, area_id=None, lat=None,
                                   lng=None, radius_m=5000, limit=20)
        quotes = [
            {"slug": q.slug, "name": q.venue_name, "area": q.area, "menu_name": q.menu_name,
             "price_pkr": q.price_pkr}
            for q in result.quotes[:10]
        ]
        self.dishes = quotes
        for q in quotes:
            self.seen.setdefault(q["slug"], {"slug": q["slug"], "name": q["name"]})
        return {"dish": result.family, "median_pkr": result.median_pkr, "cheapest": quotes,
                "venues_pricing_it": result.venue_count}

    async def get_deals(self, bank: str | None = None, area: str | None = None, **_) -> list:
        rows = await deals_service.search(self.ctx.session, bank=bank, area=area or self.area,
                                          limit=10)
        for r in rows:
            self.seen.setdefault(r["venue"]["slug"], {"slug": r["venue"]["slug"],
                                                      "name": r["venue"]["name"]})
        return [{"bank": r["bank"], "discount_pct": r["discount_pct"], "cap_pkr": r["cap"],
                 "venue": r["venue"]["name"], "slug": r["venue"]["slug"],
                 "area": r["venue"]["area"], "valid_today": r["today"]} for r in rows]

    def tool(self, name: str) -> Callable[..., Awaitable] | None:
        return {
            "search_venues": self.search_venues, "get_venue": self.get_venue,
            "get_live": self.get_live, "get_forecast": self.get_forecast,
            "compare_dish_prices": self.compare_dish_prices, "get_deals": self.get_deals,
            "get_menu": self.get_menu,
        }.get(name)

    async def run_tool(self, name: str, args: dict) -> dict:
        fn = self.tool(name)
        self.tool_calls.append(name)
        if fn is None:
            return {"error": f"unknown tool {name}"}
        try:
            result = await fn(**(args or {}))
        except Exception as exc:  # noqa: BLE001 — a tool failure is reported to the model
            log.warning("chat tool %s failed: %s", name, exc)
            return {"error": "lookup failed"}
        return {"result": result}


def _uuid(value):
    import uuid

    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


# --- the turn ------------------------------------------------------------------


def _contents(messages: list[dict], context: str) -> list[dict]:
    out = []
    for m in messages[-12:]:
        role = "model" if m["role"] in ("assistant", "model") else "user"
        out.append({"role": role, "parts": [{"text": m["content"][:2000]}]})
    # The context rides on the last user turn, so it is about the question being asked now.
    out[-1]["parts"].insert(0, {"text": context})
    return out


async def _model_turn(agent: Agent, messages: list[dict], context: str) -> str | None:
    contents = _contents(messages, context)
    deadline = time.monotonic() + TOTAL_BUDGET_S
    # The model that wrote a tool call is asked first for the next round: the call carries its
    # thought signature, which only it accepts. The chain strips it for any other model.
    model = None
    for _ in range(MAX_TOOL_ROUNDS + 1):
        remaining = deadline - time.monotonic()
        if remaining < 2:
            return None
        payload = await llm.call_raw(
            {"system_instruction": {"parts": [{"text": SYSTEM}]}, "contents": contents,
             "tools": TOOLS, "generationConfig": {"maxOutputTokens": 600, "temperature": 0.4}},
            model=model, timeout=min(PER_CALL_S, remaining),
        )
        if payload is None:
            return None
        model = payload.get("_model")
        content = payload["candidates"][0].get("content") or {}
        parts = content.get("parts") or []
        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        if not calls:
            return llm.text_of(payload).strip() or None
        # The model's turn goes back verbatim: its thought signatures must accompany it.
        contents.append({"role": "model", "parts": parts})
        results = await asyncio.gather(
            *(agent.run_tool(c.get("name", ""), c.get("args") or {}) for c in calls)
        )
        contents.append({
            "role": "user",
            "parts": [
                {"functionResponse": {"name": c.get("name", ""), "response": r,
                                      **({"id": c["id"]} if c.get("id") else {})}}
                for c, r in zip(calls, results, strict=True)
            ],
        })
    return None


async def _grounded(question: str, roman: bool) -> tuple[str | None, list[dict]]:
    """Google Search grounding for a question our data cannot answer. Best effort."""
    payload = await llm.call_raw(
        {"system_instruction": {"parts": [{"text": SYSTEM}]},
         "contents": [{"role": "user", "parts": [{"text": question}]}],
         "tools": [{"google_search": {}}],
         "generationConfig": {"maxOutputTokens": 400}},
        timeout=PER_CALL_S,
    )
    if payload is None:
        return None, []
    meta = payload["candidates"][0].get("groundingMetadata") or {}
    sources = [
        {"title": c["web"].get("title"), "url": c["web"].get("uri")}
        for c in meta.get("groundingChunks") or [] if c.get("web")
    ][:5]
    return llm.text_of(payload).strip() or None, sources


def wait_phrase(minutes: int | float | None, roman: bool) -> str:
    """Never "about 0 min wait"."""
    m = round(minutes or 0)
    if m <= 2:
        return "abhi koi wait nahi" if roman else "no wait right now"
    return f"takreeban {m} min wait" if roman else f"about {m} min wait"


def menu_reply(name: str, items: list[dict], roman: bool) -> str:
    """A menu answer from the menu alone, for when no model is available."""
    priced = [i for i in items if i.get("price_pkr")][:6]
    if not priced:
        return (f"{name} ka menu abhi humare paas nahi hai." if roman
                else f"We don't have {name}'s menu yet.")
    lines = "; ".join(
        f"{i['name']} Rs {i['price_pkr']:,}" + (f" ({i['portion']})" if i.get("portion") else "")
        for i in priced
    )
    return f"{name} ke kuch items: {lines}." if roman else f"Some of {name}'s menu: {lines}."


def template_reply(rows: list[dict], roman: bool) -> str:
    """The answer from the numbers alone, in the same plain words the model is given."""
    if not rows:
        return ("Is waqt is hisaab se koi jagah nahi mili. Area ya budget thoda badal kar "
                "dekhein." if roman else
                "Nothing matched that right now. Try a nearby area or a higher budget.")
    top = rows[0]
    status = top.get("status") or ""
    if status and "wait" not in status:
        status += ", " + wait_phrase(top.get("wait_min"), roman)
    deal = f" {top['deals'][0]}." if top.get("deals") else ""
    where = f"{top['name']} ({top['area']})" if top.get("area") else top["name"]
    text = f"{where}: {status}.{deal}" if status else f"{where}.{deal}"
    if len(rows) > 1:
        text += (f" Doosra option: {rows[1]['name']}." if roman
                 else f" Also worth a look: {rows[1]['name']}.")
    return text


def suggestions(roman: bool, has_venues: bool) -> list[str]:
    if roman:
        base = ["Sirf abhi khali jagah dikhao", "Rs 1000 tak", "Raat 9 baje kitna rush hoga?"]
        return base if has_venues else ["Bahadurabad mein biryani", "Clifton mein BBQ"]
    base = ["Only places free right now", "Under Rs 1,000", "How busy at 9 pm?"]
    return base if has_venues else ["Biryani in Bahadurabad", "BBQ in Clifton"]


_DEICTIC = re.compile(
    r"\b(yahan|yahaan|wahan|wahaan|idhar|udhar|is restaurant|us restaurant|is jagah|us jagah|"
    r"iska|uska|iski|uski|inka|unka|here|there|this place|that place|this restaurant|"
    r"that restaurant|their)\b"
)
_MENU = re.compile(
    r"\b(menu|kya milta|kya kya|dishes|items|price|prices|rate|rates|kitne ka|kitne ki|"
    r"kitna|serve|khana kya)\b"
)


def is_menu_question(text: str) -> bool:
    return bool(_MENU.search(text.lower()))


def refers_back(text: str) -> bool:
    return bool(_DEICTIC.search(text.lower()))


async def focus_venue(ctx: AuthContext, messages: list[dict],
                      explicit: str | None = None,
                      on_screen: list[str] | None = None) -> dict | None:
    """The venue the conversation is about: the one the client names, else the most recent
    one named in the conversation, this message included."""
    from . import names

    if explicit:
        vid = await lookup.venue_id_for(ctx.session, explicit)
        if vid:
            return await _venue_brief(ctx, vid)
    # The question itself first ("China Town ka menu"), then the cards the app showed with the
    # last answer, then whatever the conversation named most recently.
    # Only an exact name counts in the question itself: a loose word match on "Yahan ka menu
    # btao" finds some venue with one of those words in its name, which is never the point.
    exact = await named_exactly(ctx, messages[-1]["content"])
    if exact:
        return await _venue_brief(ctx, exact)
    for ident in (on_screen or [])[:5]:
        vid = await lookup.venue_id_for(ctx.session, ident)
        if vid:
            return await _venue_brief(ctx, vid)
    for m in reversed(messages[-10:-1]):
        content = m["content"][:600]
        exact = await named_exactly(ctx, content)
        if exact:
            return await _venue_brief(ctx, exact)
        ids = await names.venues_named_in(ctx.session, content[:280], limit=5)
        if not ids:
            continue
        if m["role"] == "user":
            return await _venue_brief(ctx, ids[0])
        # A reply is long and full of area and cuisine words, so a loose match on it finds
        # "China Beijing Beauty Parlour Clifton" for a sentence about China Town in Clifton.
        # From a reply, only a venue whose whole name is in the text counts.
        found = await ctx.session.execute(
            text("SELECT id, name FROM venue WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": [str(i) for i in ids]},
        )
        lowered = content.lower()
        exact = sorted((r for r in found.all() if r[1].lower() in lowered),
                       key=lambda r: lowered.index(r[1].lower()))
        if exact:
            return await _venue_brief(ctx, exact[0][0])
    return None


async def named_exactly(ctx: AuthContext, question: str):
    """A venue whose whole name, branch suffix aside, is in the question; the longest wins.

    The word-rarity match in services/names.py skips common words, so "Allah Wala Biryani"
    (three words that each appear in dozens of names) matched nothing and the chat said the
    hero venue did not exist. A full-name containment test has no such blind spot.
    """
    row = (
        await ctx.session.execute(
            text(
                r"""
                SELECT id FROM (
                    SELECT v.id, v.google_review_count,
                           lower(trim(regexp_replace(v.name, '\s*[-|(,].*$', ''))) AS base
                      FROM venue v WHERE v.status = 'active'
                ) x
                 WHERE length(base) >= 8 AND position(' ' IN base) > 0
                   AND position(base IN lower(:q)) > 0
                 ORDER BY length(base) DESC, google_review_count DESC NULLS LAST
                 LIMIT 1
                """
            ),
            {"q": question[:400]},
        )
    ).first()
    return row[0] if row else None


async def _venue_brief(ctx: AuthContext, venue_id) -> dict | None:
    from ..routers.venues import venue_card

    try:
        card = await venue_card(str(venue_id), ctx)
    except Exception:  # noqa: BLE001
        return None
    live = card.live
    return {
        "slug": card.slug, "name": card.name, "area": card.area.name if card.area else None,
        "status": status_phrase(live.band, live.wait_p50_min) if live else None,
        "measured_now": bool(live and live.source == "live"),
        "wait_min": round(live.wait_p50_min or 0) if live else 0,
        "deals": [f"{d.bank} {d.discount_pct}% off" for d in card.deals[:3]],
        "offers": [o["title"] for o in card.offers[:3]],
    }


async def respond(ctx: AuthContext, messages: list[dict], *, lat: float | None,
                  lng: float | None, area: str | None, cards: list[str],
                  venue: str | None = None, context_venues: list[str] | None = None) -> dict:
    question = messages[-1]["content"]
    roman = is_roman_urdu(question)
    _ROMAN.set(roman)
    cards = [deals_service.canonical_bank(c) for c in cards if c] or banks_in(question)
    agent = Agent(ctx, lat=lat, lng=lng, area=area, cards=cards)

    focus = None
    try:
        focus = await focus_venue(ctx, messages, venue, context_venues)
    except Exception as exc:  # noqa: BLE001
        log.warning("chat focus lookup failed: %s", exc)
    asks_menu = is_menu_question(question)
    # "Yahan ka menu btao" is about the venue already on the table, not a new search.
    # A menu question without "yahan" is still a follow-up when it names the venue ("China
    # Town ka menu") or is too short to be a new search ("menu btao"); "Bahadurabad mein
    # biryani kitne ki" is a new search and keeps the normal path.
    named_now = bool(focus) and focus["name"].lower().split(" ")[0] in question.lower()
    # "Allah Wala Biryani abhi jana theek rahega?" is a question about that venue, not a
    # search that may or may not rank it.
    asked_by_name = bool(focus) and (await named_exactly(ctx, question)) is not None
    about_focus = bool(focus) and (
        refers_back(question) or venue is not None or asked_by_name
        or (asks_menu and (named_now or len(question.split()) <= 5))
    )

    rows: list[dict] = []
    menu: dict | None = None
    if about_focus:
        rows = [focus]
        agent.seen[focus["slug"]] = focus
        if asks_menu:
            menu = await agent.get_menu(focus["slug"])
    else:
        # The deterministic answer first: it is the context for the model and the fallback.
        try:
            rows = await agent.search_venues(query=question, area=area,
                                             card_bank=cards[0] if cards else None)
        except Exception as exc:  # noqa: BLE001
            log.warning("chat pre-search failed: %s", exc)
    agent.tool_calls.clear()

    context = (
        (f"FOCUS (the venue this conversation is about): "
         f"{json.dumps(focus, ensure_ascii=False)}\n" if focus else "")
        + (f"MENU of {focus['name']}: {json.dumps(menu, ensure_ascii=False)}\n"
           if menu and focus else "")
        + "CONTEXT (live results for the user's latest message, best first; you may call "
        "tools for anything else):\n" + json.dumps(rows, ensure_ascii=False)
        + (f"\nUser's bank cards: {', '.join(cards)}" if cards else "")
        + f"\nKarachi time now: {local_now().strftime('%a %H:%M')}"
    )

    reply, source, sources = None, "template", []
    if llm.available():
        try:
            reply = await _model_turn(agent, messages, context)
        except Exception as exc:  # noqa: BLE001 — the template must still answer
            log.warning("chat model turn failed: %s", exc)
            reply = None
        if reply:
            source = "model"
        if not rows and not agent.seen:
            grounded, sources = await _grounded(question, roman)
            if grounded:
                reply, source = grounded, "model"
    if not reply:
        if menu is not None and focus:
            reply = menu_reply(focus["name"], menu.get("items") or [], roman)
        else:
            reply = template_reply(rows, roman)

    # The cards: venues the reply names, in the order it names them, then the top results.
    # Only venues a tool returned can appear here, whatever the text says.
    lowered = reply.lower()
    named = sorted((s for s, r in agent.seen.items() if r["name"].lower() in lowered),
                   key=lambda s: lowered.index(agent.seen[s]["name"].lower()))
    slugs = (named + [r["slug"] for r in rows if r["slug"] not in named])[:CARD_LIMIT]

    from ..routers.venues import venue_card

    cards_out = []
    for slug in slugs:
        try:
            cards_out.append(await venue_card(slug, ctx))
        except Exception:  # noqa: BLE001 — a vanished venue is just left out
            continue

    return {
        "reply": reply,
        "venues": [c.model_dump(mode="json") for c in cards_out],
        "dishes": agent.dishes or None,
        "menus": agent.menus or None,
        "sources": sources or None,
        "suggestions": suggestions(roman, bool(cards_out)),
        "reply_source": source,
    }
