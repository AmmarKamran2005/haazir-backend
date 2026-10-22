"""HAAZIR Ghar: a meal plan with its grocery list, and a dawat calculator. Plan §4 B8.

Both ask Gemini for JSON and both have a complete answer without it. The fallback is not a
placeholder: it is a rotation of ordinary Karachi home cooking scaled to the household, with
quantities from the same per-person rules a cook would use, so the screen is always full and
always plausible. Nothing is stored; the app keeps the result on the phone.

**What the user asked for is what comes back.** A dawat returns one line per dish on the
menu, named as the user named it, and nothing else; a model answer that adds a dish or drops
one is repaired against the menu rather than trusted. A meal plan's grocery list always
carries the household staples, scaled to people and days, whatever the model remembered.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from ..services import llm
from ..services.clock import local_now

router = APIRouter(prefix="/v1/ghar", tags=["ghar"])

Protein = Literal["chicken", "mutton", "beef", "fish", "none"]
Meal = Literal["breakfast", "lunch", "dinner"]


# --- shapes ------------------------------------------------------------------


class PantryItem(BaseModel):
    item: str = Field(min_length=1, max_length=60)
    qty: float = Field(ge=0, le=10_000)
    unit: str = Field(default="kg", max_length=12)


class MealPlanIn(BaseModel):
    people: int = Field(ge=1, le=30)
    days: Literal[7, 15, 30] = 7
    # Which meals to plan. Lunch and dinner when not given, which is what older apps expect.
    meals: list[Meal] = Field(default_factory=lambda: ["lunch", "dinner"], max_length=3)
    budget_pkr: int | None = Field(default=None, ge=500, le=2_000_000)
    likes: list[str] = Field(default_factory=list, max_length=20)
    dislikes: list[str] = Field(default_factory=list, max_length=20)
    diet: str | None = Field(default=None, max_length=40)
    pantry: list[PantryItem] = Field(default_factory=list, max_length=60)
    gas_hours: str | None = Field(default=None, max_length=60)

    @field_validator("meals")
    @classmethod
    def _some_meal(cls, v: list[str]) -> list[str]:
        order = ["breakfast", "lunch", "dinner"]
        return [m for m in order if m in v] or ["lunch", "dinner"]


class DawatDish(BaseModel):
    dish: str = Field(min_length=1, max_length=60)
    protein: Protein | None = None


class DawatIn(BaseModel):
    guests: int = Field(ge=2, le=1000)
    # Plain names (older apps) or {dish, protein}; both are accepted in the same list.
    menu: list[DawatDish | str] = Field(min_length=1, max_length=15)
    style: Literal["desi", "bbq", "mixed"] = "desi"

    def dishes(self) -> list[DawatDish]:
        out = []
        for d in self.menu:
            item = DawatDish(dish=d) if isinstance(d, str) else d
            if item.dish.strip():
                out.append(DawatDish(dish=item.dish.strip(), protein=item.protein))
        return out


# --- prices ------------------------------------------------------------------

# Rough Karachi retail, October 2026: unit, PKR per unit.
PRICES = {
    "chicken": ("kg", 750), "beef": ("kg", 1300), "mutton": ("kg", 2600), "fish": ("kg", 1400),
    "rice": ("kg", 420), "atta": ("kg", 150), "daal": ("kg", 380), "chanay": ("kg", 340),
    "potato": ("kg", 110), "onion": ("kg", 160), "tomato": ("kg", 180),
    "yogurt": ("kg", 260), "oil": ("litre", 560), "eggs": ("dozen", 380),
    "mixed vegetables": ("kg", 200), "spinach": ("kg", 120), "masala": ("pack", 140),
    "milk": ("litre", 230), "sugar": ("kg", 165), "tea leaves": ("kg", 1800),
    "salt": ("kg", 60), "spices": ("kg", 1600), "bread": ("loaf", 180),
    "ghee": ("kg", 650), "suji": ("kg", 180), "garlic ginger": ("kg", 600),
}
PROTEIN_PRICE = {"chicken": 750, "beef": 1300, "mutton": 2600, "fish": 1400}


# --- the meal-plan fallback --------------------------------------------------

DISHES = [
    ("Chicken karahi with roti", {"chicken": 0.2, "tomato": 0.08, "atta": 0.12, "oil": 0.02}),
    ("Daal chawal", {"daal": 0.07, "rice": 0.1, "onion": 0.03, "oil": 0.015}),
    ("Aloo gosht with roti", {"beef": 0.15, "potato": 0.12, "atta": 0.12, "oil": 0.02}),
    ("Chicken biryani", {"chicken": 0.18, "rice": 0.15, "yogurt": 0.04, "masala": 0.1}),
    ("Saag with roti", {"spinach": 0.2, "atta": 0.12, "oil": 0.015}),
    ("Chana masala with rice", {"chanay": 0.08, "rice": 0.1, "tomato": 0.05, "oil": 0.015}),
    ("Qeema matar with roti", {"beef": 0.12, "mixed vegetables": 0.06, "atta": 0.12}),
    ("Mixed sabzi with daal", {"mixed vegetables": 0.2, "daal": 0.05, "atta": 0.1}),
    ("Chicken pulao with raita", {"chicken": 0.15, "rice": 0.15, "yogurt": 0.06}),
    ("Anday ka salan with roti", {"eggs": 0.17, "tomato": 0.06, "atta": 0.12, "onion": 0.04}),
    ("Fish curry with rice", {"fish": 0.18, "rice": 0.12, "tomato": 0.05}),
    ("Aloo keema with paratha", {"beef": 0.1, "potato": 0.1, "atta": 0.14, "oil": 0.03}),
    ("Haleem", {"beef": 0.12, "daal": 0.06, "atta": 0.03, "masala": 0.1}),
    ("Kadhi pakora with rice", {"yogurt": 0.15, "rice": 0.1, "atta": 0.03, "oil": 0.03}),
]

BREAKFASTS = [
    ("Paratha, fried egg and chai", {"atta": 0.1, "eggs": 0.085, "ghee": 0.02, "milk": 0.1}),
    ("Anda bread with chai", {"bread": 0.25, "eggs": 0.17, "milk": 0.1}),
    ("Aloo paratha with dahi", {"atta": 0.1, "potato": 0.08, "yogurt": 0.08, "ghee": 0.02}),
    ("Omelette, toast and chai", {"bread": 0.2, "eggs": 0.17, "onion": 0.02, "milk": 0.1}),
    ("Suji halwa with paratha", {"suji": 0.05, "sugar": 0.03, "ghee": 0.02, "atta": 0.08}),
    ("Chana and puri (weekend)", {"chanay": 0.05, "atta": 0.1, "oil": 0.04}),
    ("Doodh pati with rusks and fruit", {"milk": 0.25, "sugar": 0.02, "bread": 0.15}),
]

# What a Karachi kitchen goes through per person per day regardless of the menu: chai, salt,
# the onion-tomato base of every salan, the oil and the spices. Without these a grocery list
# looks complete on screen and leaves the household at the shop on day two.
STAPLES = {
    "sugar": 0.025, "tea leaves": 0.006, "milk": 0.2, "salt": 0.004, "oil": 0.012,
    "spices": 0.004, "onion": 0.06, "tomato": 0.05, "garlic ginger": 0.01,
    "atta": 0.05, "rice": 0.02, "daal": 0.01,
}
# How a staple might be written on someone else's list: "Ginger Garlic Paste", "Dal Masoor".
STAPLE_WORDS = {
    "sugar": ("sugar", "cheeni"), "tea leaves": ("tea", "chai", "patti"),
    "milk": ("milk", "doodh"), "salt": ("salt", "namak"), "oil": ("oil", "ghee"),
    "spices": ("spice", "masala", "mirch", "haldi"), "onion": ("onion", "pyaz"),
    "tomato": ("tomato", "tamatar"), "garlic ginger": ("garlic", "ginger", "lehsan", "adrak"),
    "atta": ("atta", "flour"), "rice": ("rice", "chawal"),
    "daal": ("daal", "dal ", "dal", "lentil", "masoor", "moong", "chana"),
}
MEAT = ("chicken", "beef", "mutton", "fish", "eggs")


def _avoid(dish: str, ingredients: dict, dislikes: list[str], diet: str | None) -> bool:
    words = " ".join([dish.lower(), *ingredients]).lower()
    if any(d.strip().lower() and d.strip().lower() in words for d in dislikes):
        return True
    if diet and "veg" in diet.lower():
        return any(m in ingredients for m in MEAT)
    return bool(diet and "no beef" in diet.lower() and "beef" in ingredients)


def _week(day_index: int) -> int:
    return day_index // 7 + 1


def staples_need(people: int, days: int) -> dict[tuple[str, int], float]:
    need: dict[tuple[str, int], float] = {}
    for i in range(days):
        for item, per_person in STAPLES.items():
            key = (item, _week(i))
            need[key] = need.get(key, 0.0) + per_person * people
    return need


def grocery_lines(need: dict[tuple[str, int], float], pantry: list[PantryItem]) -> list[dict]:
    """Priced lines grouped by week, with the pantry used up from the first week onward."""
    have = {p.item.strip().lower(): p.qty for p in pantry}
    lines = []
    for (item, week), qty in sorted(need.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        unit, price = PRICES[item]
        if unit == "dozen":
            qty /= 12
        elif unit == "loaf":
            qty /= 0.8  # a loaf feeds about four slices' worth of 0.2 per person
        for name in list(have):
            if name == item or name.rstrip("s") == item.rstrip("s"):
                used = min(have[name], qty)
                have[name] -= used
                qty -= used
        if qty <= 0.001:
            continue
        qty = math.ceil(qty * 4) / 4
        lines.append({"item": item.capitalize(), "qty": qty, "unit": unit,
                      "est_pkr": round(qty * price / 10) * 10, "when": f"Week {week}"})
    return lines


def fallback_plan(body: MealPlanIn, start: dt.date) -> dict:
    menu = [d for d in DISHES if not _avoid(*d, body.dislikes, body.diet)] or DISHES[:4]
    liked = [d for d in menu if any(like.lower() in d[0].lower() for like in body.likes)]
    rotation = liked + [d for d in menu if d not in liked]
    mornings = [b for b in BREAKFASTS if not _avoid(*b, body.dislikes, body.diet)] or BREAKFASTS

    days, need = [], staples_need(body.people, body.days)
    for i in range(body.days):
        planned: dict[str, tuple[str, dict]] = {}
        if "breakfast" in body.meals:
            planned["breakfast"] = mornings[i % len(mornings)]
        mains = [m for m in ("lunch", "dinner") if m in body.meals]
        for j, meal in enumerate(mains):
            planned[meal] = rotation[(len(mains) * i + j) % len(rotation)]
        # A meal that was not asked for is null; older apps always ask for lunch and dinner.
        day = {"date": (start + dt.timedelta(days=i)).isoformat(), "lunch": None, "dinner": None,
               "notes": "Cook dinner's masala at lunch to save gas." if body.gas_hours else ""}
        for meal, (name, ingredients) in planned.items():
            day[meal] = name
            for item, per_person in ingredients.items():
                key = (item, _week(i))
                need[key] = need.get(key, 0.0) + per_person * body.people
        days.append(day)

    grocery = grocery_lines(need, body.pantry)
    total = sum(g["est_pkr"] for g in grocery)
    tips = [
        "Buy rice, atta, daal and sugar for the whole period at once; they keep, and bulk is cheaper.",
        "Chicken and vegetables are cheapest on Sunday mornings at the weekly bazaar.",
    ]
    if body.budget_pkr and total > body.budget_pkr:
        tips.insert(0, f"This plan is about Rs {total - body.budget_pkr:,} over budget; swap two "
                       "meat dishes a week for daal or chana to close the gap.")
    elif body.budget_pkr:
        tips.insert(0, f"Estimated groceries Rs {total:,}, inside your Rs {body.budget_pkr:,} budget.")
    if body.gas_hours:
        tips.append(f"Gas hours {body.gas_hours}: cook both meals' base gravy together and reheat.")
    return {"days": days, "grocery": grocery, "tips": tips}


def complete_grocery(data: dict, body: MealPlanIn) -> dict:
    """A model's list plus every staple it forgot, scaled and net of the pantry."""
    grocery = [g for g in data.get("grocery") or [] if isinstance(g, dict) and g.get("item")]
    named = " ".join(str(g["item"]).lower() for g in grocery)
    missing = {k: v for k, v in staples_need(body.people, body.days).items()
               if not any(word in named for word in STAPLE_WORDS[k[0]])}
    data["grocery"] = grocery + grocery_lines(missing, body.pantry)
    for day in data.get("days") or []:
        for meal in ("breakfast", "lunch", "dinner"):
            if meal not in body.meals or not day.get(meal):
                day[meal] = None
        if "breakfast" not in body.meals:
            day.pop("breakfast", None)
        day.setdefault("notes", "")
    data.setdefault("tips", [])
    return data


# --- the dawat calculator ----------------------------------------------------

# Per guest, by kind of dish: (unit, quantity, PKR per unit, is a meat main, note).
KINDS = {
    "rice": ("kg rice", 0.15, 420, True, "Weight is uncooked rice; the meat is priced in."),
    "curry": ("kg meat", 0.22, None, True, None),
    "bbq": ("kg meat", 0.30, None, True, "Marinate overnight; count about 1 kg coal per 2 kg meat."),
    "bread": ("pieces", 2.5, 30, False, None),
    "raita": ("kg yogurt", 0.08, 260, False, None),
    "salad": ("kg vegetables", 0.08, 180, False, None),
    "sweet": ("kg", 0.12, 650, False, None),
    "drink": ("litres", 0.5, 120, False, None),
    "other": ("kg", 0.15, 700, False, None),
}
KIND_WORDS = [
    ("rice", ("biryani", "pulao", "pilaf")),
    ("bbq", ("bbq", "tikka", "boti", "kabab", "kebab", "seekh", "chops", "malai", "reshmi")),
    ("curry", ("qorma", "korma", "karahi", "nihari", "haleem", "salan", "gosht", "qeema",
               "keema", "paye", "kofta", "handi", "jalfrezi", "white")),
    ("bread", ("roti", "naan", "tandoori", "paratha", "sheermal", "taftan", "puri")),
    ("raita", ("raita", "dahi")),
    ("salad", ("salad", "kachumber")),
    ("sweet", ("kheer", "zarda", "rabri", "rabdi", "custard", "dessert", "firni", "gajar",
               "halwa", "gulab", "jamun", "trifle", "shahi", "tukray", "ice cream", "mithai",
               "sheer")),
    ("drink", ("drink", "juice", "lassi", "doodh", "sharbat", "soda", "cola", "water")),
]
# Some sweets are priced differently from a plain kheer: rabri and mithai are milk-heavy.
SWEET_PRICE = {"rabri": 1100, "rabdi": 1100, "mithai": 1400, "gulab": 900, "jamun": 900,
               "ice cream": 800, "zarda": 550, "kheer": 600, "firni": 600}
# How the style moves portions: a BBQ crowd eats more meat and less rice.
STYLE_FACTOR = {
    "desi": {"rice": 1.0, "curry": 1.0, "bbq": 0.85},
    "bbq": {"rice": 0.8, "curry": 0.85, "bbq": 1.2},
    "mixed": {"rice": 0.9, "curry": 0.95, "bbq": 1.0},
}
STYLE_NOTE = {
    "desi": "Desi spread: plan one deg (about 40 portions) per main dish.",
    "bbq": "BBQ night: meat portions are up; book a grill and a tandoor for fresh naan.",
    "mixed": "Mixed menu: portions of each main are trimmed because guests take some of each.",
}


def kind_of(dish: str) -> str:
    name = dish.lower()
    for kind, words in KIND_WORDS:
        if any(re.search(rf"\b{re.escape(w)}", name) for w in words):
            return kind
    return "other"


def protein_of(dish: DawatDish) -> str | None:
    if dish.protein and dish.protein != "none":
        return dish.protein
    name = dish.dish.lower()
    for p in ("mutton", "beef", "chicken", "fish"):
        if p in name:
            return p
    if "gosht" in name or "nihari" in name or "paye" in name:
        return "beef"
    return "chicken" if kind_of(dish.dish) in ("rice", "curry", "bbq") else None


def fallback_dawat(body: DawatIn) -> dict:
    dishes = body.dishes()
    mains = [d for d in dishes if KINDS[kind_of(d.dish)][3]]
    # The more mains, the less of each: guests take some of every dish, not a full plate each.
    share = 1 / (1 + 0.2 * max(0, len(mains) - 1))
    factors = STYLE_FACTOR[body.style]

    items = []
    for d in dishes:
        kind = kind_of(d.dish)
        unit, per_guest, price, is_main, note = KINDS[kind]
        protein = protein_of(d) if is_main else None
        per_guest *= factors.get(kind, 1.0) * (share if is_main else 1.0)
        qty = body.guests * per_guest * 1.1  # a caterer's margin
        qty = math.ceil(qty) if unit in ("pieces", "litres") else math.ceil(qty * 2) / 2

        if kind == "rice":
            meat = math.ceil(body.guests * 0.2 * factors["rice"] * share * 1.1 * 2) / 2
            cost = qty * price + meat * PROTEIN_PRICE.get(protein or "chicken", 750)
            note = f"Plus about {meat:g} kg {protein or 'chicken'}."
        elif kind in ("curry", "bbq"):
            cost = qty * PROTEIN_PRICE.get(protein or "chicken", 750) * 1.25  # masala, oil, ghee
        elif kind == "sweet":
            sweet_price = next((v for k, v in SWEET_PRICE.items() if k in d.dish.lower()), price)
            cost = qty * sweet_price
        else:
            cost = qty * price
        item = {"dish": d.dish, "qty": qty, "unit": unit, "est_pkr": round(cost / 50) * 50}
        if protein:
            item["protein"] = protein
        if note:
            item["note"] = note
        items.append(item)

    total = sum(i["est_pkr"] for i in items)
    degs = math.ceil(body.guests / 40)
    notes = (f"{STYLE_NOTE[body.style]} About {degs} deg{'s' if degs > 1 else ''} per main "
             "dish for this many guests. Quantities include a 10% margin; Karachi caterers "
             "usually charge Rs 2,500-4,000 per deg for cooking.")
    return {"items": items, "total_pkr": total, "notes": notes, "style": body.style}


def align_dawat(data: dict, body: DawatIn) -> dict | None:
    """Exactly the menu, in its order, named as the user named it. None if unusable.

    A model that answers "kheer" for a rabri, or adds a dessert nobody asked for, is
    corrected here: each requested dish takes the model's closest line or, failing that,
    the fallback's, and anything else the model added is dropped.
    """
    raw = [i for i in data.get("items") or [] if isinstance(i, dict) and i.get("dish")]
    if not raw:
        return None
    fallback = {i["dish"]: i for i in fallback_dawat(body)["items"]}
    used: set[int] = set()
    items = []
    for d in body.dishes():
        want = d.dish.lower()
        match = next((k for k, i in enumerate(raw) if k not in used
                      and (want in str(i["dish"]).lower() or str(i["dish"]).lower() in want)),
                     None)
        base = fallback[d.dish]
        if match is None:
            items.append(base)
            continue
        used.add(match)
        m = raw[match]
        try:
            qty, cost = float(m.get("qty")), int(m.get("est_pkr"))
        except (TypeError, ValueError):
            items.append(base)
            continue
        item = {"dish": d.dish, "qty": qty, "unit": str(m.get("unit") or base["unit"]),
                "est_pkr": cost}
        if base.get("protein"):
            item["protein"] = base["protein"]
        # The fallback's note quotes its own quantities; it only fits a line in the same unit.
        if base.get("note") and item["unit"] == base["unit"]:
            item["note"] = base["note"]
        items.append(item)
    return {"items": items, "total_pkr": sum(i["est_pkr"] for i in items),
            "notes": str(data.get("notes") or fallback_dawat(body)["notes"]),
            "style": body.style}


# --- endpoints ---------------------------------------------------------------

_PLAN_SYSTEM = (
    "You plan home meals for a Karachi household. Return JSON only, shaped exactly as "
    '{"days":[{"date":"YYYY-MM-DD","breakfast":str,"lunch":str,"dinner":str,"notes":str}],'
    '"grocery":[{"item":str,"qty":number,"unit":str,"est_pkr":int,"when":"Week N"}],'
    '"tips":[str]}. Plan only the meals listed in MEALS; use null for any other meal. Ordinary '
    "Pakistani home cooking and realistic Karachi prices in PKR. The grocery list MUST "
    "include household staples scaled to people and days (sugar, tea leaves, milk, cooking "
    "oil, atta, rice, daal, salt, spices, onion, tomato, garlic-ginger, eggs if eaten) as well "
    "as the ingredients of every dish, minus the pantry, grouped by week. Respect dislikes and "
    "diet; stay within the budget if given. Dish names in English or Roman Urdu. No commentary."
)

_DAWAT_SYSTEM = (
    "You are a Karachi caterer sizing a dawat. Return JSON only, shaped exactly as "
    '{"items":[{"dish":str,"qty":number,"unit":str,"est_pkr":int}],"total_pkr":int,'
    '"notes":str}. Return EXACTLY one item per dish in MENU, in the same order, with the '
    "dish name exactly as given; never add or rename a dish. Size each dish for the guests "
    "with its stated protein (mutton costs about Rs 2,600/kg, beef 1,300, chicken 750) and "
    "the STYLE (bbq: more meat, less rice; mixed: smaller portions of each main). Units: kg, "
    "pieces or litres; realistic Karachi prices in PKR with a 10% margin. No commentary."
)


def _valid_plan(data, days: int) -> bool:
    return (isinstance(data, dict) and isinstance(data.get("days"), list)
            and len(data["days"]) >= days // 2 and isinstance(data.get("grocery"), list)
            and all(isinstance(d, dict) and ("lunch" in d or "dinner" in d or "breakfast" in d)
                    for d in data["days"]))


@router.post("/meal-plan")
async def meal_plan(body: MealPlanIn) -> dict:
    start = local_now().date() + dt.timedelta(days=1)
    data = await llm.generate_json(
        _PLAN_SYSTEM,
        f"Start date: {start.isoformat()}\nMEALS: {', '.join(body.meals)}\n"
        + body.model_dump_json(),
        max_tokens=8192 if body.days > 7 else 4096,
        timeout=25.0,
    )
    if _valid_plan(data, body.days):
        return complete_grocery(data, body)
    return fallback_plan(body, start)


@router.post("/dawat")
async def dawat(body: DawatIn) -> dict:
    menu = [{"dish": d.dish, "protein": protein_of(d)} for d in body.dishes()]
    data = await llm.generate_json(
        _DAWAT_SYSTEM,
        f"GUESTS: {body.guests}\nSTYLE: {body.style}\nMENU: {menu}",
        max_tokens=1500, timeout=12.0,
    )
    aligned = align_dawat(data, body) if isinstance(data, dict) else None
    return aligned or fallback_dawat(body)
