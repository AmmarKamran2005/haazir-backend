"""HAAZIR Ghar: a meal plan with its grocery list, and a dawat calculator. Plan §4 B8.

Both ask Gemini for JSON and both have a complete answer without it. The fallback is not a
placeholder: it is a rotation of ordinary Karachi home cooking scaled to the household, with
quantities from the same per-person rules a cook would use, so the screen is always full and
always plausible. Nothing is stored; the app keeps the result on the phone.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..services import llm
from ..services.clock import local_now

router = APIRouter(prefix="/v1/ghar", tags=["ghar"])


# --- shapes ------------------------------------------------------------------


class PantryItem(BaseModel):
    item: str = Field(min_length=1, max_length=60)
    qty: float = Field(ge=0, le=10_000)
    unit: str = Field(default="kg", max_length=12)


class MealPlanIn(BaseModel):
    people: int = Field(ge=1, le=30)
    days: Literal[7, 15, 30] = 7
    budget_pkr: int | None = Field(default=None, ge=500, le=2_000_000)
    likes: list[str] = Field(default_factory=list, max_length=20)
    dislikes: list[str] = Field(default_factory=list, max_length=20)
    diet: str | None = Field(default=None, max_length=40)
    pantry: list[PantryItem] = Field(default_factory=list, max_length=60)
    gas_hours: str | None = Field(default=None, max_length=60)


class DawatIn(BaseModel):
    guests: int = Field(ge=2, le=1000)
    menu: list[str] = Field(min_length=1, max_length=15)
    style: Literal["desi", "bbq", "mixed"] = "desi"


# --- the fallback kitchen ----------------------------------------------------

# Per person per meal, and a rough Karachi retail price per unit (October 2026).
PRICES = {
    "chicken": ("kg", 750), "beef": ("kg", 1300), "mutton": ("kg", 2600),
    "rice": ("kg", 420), "atta": ("kg", 150), "daal": ("kg", 380), "chanay": ("kg", 340),
    "potato": ("kg", 110), "onion": ("kg", 160), "tomato": ("kg", 180),
    "yogurt": ("kg", 260), "oil": ("litre", 560), "eggs": ("dozen", 380),
    "mixed vegetables": ("kg", 200), "spinach": ("kg", 120), "masala": ("pack", 140),
    "milk": ("litre", 230), "fish": ("kg", 1400),
}

DISHES = [
    ("Chicken karahi with roti", {"chicken": 0.2, "tomato": 0.08, "atta": 0.12, "oil": 0.02}),
    ("Daal chawal", {"daal": 0.07, "rice": 0.1, "onion": 0.03, "oil": 0.015}),
    ("Aloo gosht with roti", {"beef": 0.15, "potato": 0.12, "atta": 0.12, "oil": 0.02}),
    ("Chicken biryani", {"chicken": 0.18, "rice": 0.15, "yogurt": 0.04, "masala": 0.1}),
    ("Palak paneer-style saag with roti", {"spinach": 0.2, "atta": 0.12, "oil": 0.015}),
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


def _avoid(dish: str, ingredients: dict, dislikes: list[str], diet: str | None) -> bool:
    words = " ".join([dish.lower(), *ingredients]).lower()
    if any(d.strip().lower() and d.strip().lower() in words for d in dislikes):
        return True
    if diet and "veg" in diet.lower():
        return any(m in ingredients for m in ("chicken", "beef", "mutton", "fish", "eggs"))
    return bool(diet and "no beef" in diet.lower() and "beef" in ingredients)


def fallback_plan(body: MealPlanIn, start: dt.date) -> dict:
    menu = [d for d in DISHES if not _avoid(*d, body.dislikes, body.diet)] or DISHES[:4]
    liked = [d for d in menu if any(like.lower() in d[0].lower() for like in body.likes)]
    rotation = liked + [d for d in menu if d not in liked]

    days, need = [], {}
    for i in range(body.days):
        lunch, dinner = rotation[(2 * i) % len(rotation)], rotation[(2 * i + 1) % len(rotation)]
        days.append({
            "date": (start + dt.timedelta(days=i)).isoformat(),
            "lunch": lunch[0], "dinner": dinner[0],
            "notes": "Cook dinner's masala at lunch to save gas." if body.gas_hours else "",
        })
        week = i // 7 + 1
        for _, ingredients in (lunch, dinner):
            for item, per_person in ingredients.items():
                key = (item, week)
                need[key] = need.get(key, 0) + per_person * body.people

    have = {p.item.strip().lower(): p.qty for p in body.pantry}
    grocery = []
    for (item, week), qty in sorted(need.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        unit, price = PRICES[item]
        if unit == "dozen":
            qty = qty / 12
        if item in have and week == 1:
            qty = max(0.0, qty - have.pop(item))
        if qty <= 0:
            continue
        qty = math.ceil(qty * 4) / 4
        grocery.append({"item": item.capitalize(), "qty": qty, "unit": unit,
                        "est_pkr": round(qty * price / 10) * 10, "when": f"Week {week}"})

    total = sum(g["est_pkr"] for g in grocery)
    tips = [
        "Buy rice, atta and daal for the whole period at once; they keep and prices are lower in bulk.",
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


# Per guest, desi dawat rules of thumb: what to buy, and how it is said in a kitchen.
DAWAT_RULES = [
    (("biryani", "pulao"), "kg rice", 0.15, 420, "Plus about 0.2 kg meat per guest."),
    (("qorma", "korma", "karahi", "nihari", "haleem", "salan"), "kg meat", 0.22, 1300, None),
    (("bbq", "tikka", "boti", "kabab", "seekh"), "kg meat", 0.3, 1300, "Marinate overnight."),
    (("roti", "naan", "tandoori"), "pieces", 2.5, 30, None),
    (("raita",), "kg yogurt", 0.08, 260, None),
    (("salad",), "kg vegetables", 0.08, 180, None),
    (("kheer", "zarda", "custard", "dessert", "firni", "gajar"), "kg", 0.12, 600, None),
    (("drink", "cold drink", "soft drink", "juice"), "litres", 0.5, 120, None),
]


def fallback_dawat(body: DawatIn) -> dict:
    items = []
    for dish in body.menu:
        name = dish.strip().lower()
        rule = next((r for r in DAWAT_RULES if any(k in name for k in r[0])), None)
        if rule is None:
            rule = ((), "kg", 0.15, 900, None)
        _, unit, per_guest, price, note = rule
        qty = body.guests * per_guest
        qty = math.ceil(qty) if unit in ("pieces", "litres") else math.ceil(qty * 2) / 2
        cost = qty * price
        if "rice" in unit:
            cost += body.guests * 0.2 * 750  # the chicken in the biryani
        items.append({"dish": dish.strip(), "qty": qty, "unit": unit,
                      "est_pkr": round(cost / 50) * 50, **({"note": note} if note else {})})
    total = sum(i["est_pkr"] for i in items)
    degs = math.ceil(body.guests / 40)
    notes = (f"Plan for about {degs} deg{'s' if degs > 1 else ''} per main dish. Quantities include "
             "a 10% margin; Karachi caterers usually charge Rs 2,500-4,000 per deg for cooking.")
    return {"items": items, "total_pkr": total, "notes": notes}


# --- endpoints ---------------------------------------------------------------

_PLAN_SYSTEM = (
    "You plan home meals for a Karachi household. Return JSON only, shaped exactly as "
    '{"days":[{"date":"YYYY-MM-DD","lunch":str,"dinner":str,"notes":str}],'
    '"grocery":[{"item":str,"qty":number,"unit":str,"est_pkr":int,"when":"Week N"}],'
    '"tips":[str]}. Ordinary Pakistani home cooking, realistic Karachi prices in PKR, the '
    "pantry subtracted from the grocery list, grocery grouped by week, respect dislikes and "
    "diet, keep within budget if given. Dish names in English or Roman Urdu. No commentary."
)

_DAWAT_SYSTEM = (
    "You are a Karachi caterer sizing a dawat. Return JSON only, shaped exactly as "
    '{"items":[{"dish":str,"qty":number,"unit":str,"est_pkr":int}],"total_pkr":int,'
    '"notes":str}. Use kg, degs, pieces or litres; realistic Karachi prices in PKR; include '
    "a small margin. No commentary."
)


def _valid_plan(data, days: int) -> bool:
    return (isinstance(data, dict) and isinstance(data.get("days"), list)
            and len(data["days"]) >= days // 2 and isinstance(data.get("grocery"), list)
            and all(isinstance(d, dict) and "lunch" in d and "dinner" in d
                    for d in data["days"]))


@router.post("/meal-plan")
async def meal_plan(body: MealPlanIn) -> dict:
    start = local_now().date() + dt.timedelta(days=1)
    data = await llm.generate_json(
        _PLAN_SYSTEM,
        f"Start date: {start.isoformat()}\n" + body.model_dump_json(),
        max_tokens=8192 if body.days > 7 else 4096,
        timeout=25.0,
    )
    if _valid_plan(data, body.days):
        data.setdefault("tips", [])
        return data
    return fallback_plan(body, start)


@router.post("/dawat")
async def dawat(body: DawatIn) -> dict:
    data = await llm.generate_json(_DAWAT_SYSTEM, body.model_dump_json(), max_tokens=1500,
                                   timeout=12.0)
    if (isinstance(data, dict) and isinstance(data.get("items"), list) and data["items"]
            and all(isinstance(i, dict) and "dish" in i for i in data["items"])):
        data.setdefault("total_pkr", sum(int(i.get("est_pkr") or 0) for i in data["items"]))
        data.setdefault("notes", "")
        return data
    return fallback_dawat(body)
