"""Ghar works without a model: the fallbacks are complete answers."""

from __future__ import annotations

import datetime as dt

from httpx import ASGITransport, AsyncClient

from haazir.routers.ghar import DawatIn, MealPlanIn, PantryItem, fallback_dawat, fallback_plan


def test_a_week_for_five_has_every_meal_and_a_priced_list():
    body = MealPlanIn(people=5, days=7, budget_pkr=25_000, dislikes=["fish"],
                      pantry=[PantryItem(item="rice", qty=5, unit="kg")])
    out = fallback_plan(body, dt.date(2026, 10, 25))
    assert len(out["days"]) == 7 and all(d["lunch"] and d["dinner"] for d in out["days"])
    assert not any("Fish" in d["lunch"] + d["dinner"] for d in out["days"])
    assert all(g["est_pkr"] > 0 for g in out["grocery"])
    assert "Rice" not in {g["item"] for g in out["grocery"] if g["when"] == "Week 1"}
    assert out["tips"]


def test_a_vegetarian_month_has_no_meat():
    out = fallback_plan(MealPlanIn(people=2, days=30, diet="vegetarian"), dt.date(2026, 11, 1))
    assert len(out["days"]) == 30
    assert {g["item"] for g in out["grocery"]}.isdisjoint({"Chicken", "Beef", "Fish", "Eggs"})


def test_a_dawat_for_eighty():
    out = fallback_dawat(DawatIn(guests=80, menu=["Chicken biryani", "Qorma", "Naan", "Kheer"]))
    units = {i["dish"]: i["unit"] for i in out["items"]}
    assert units["Naan"] == "pieces" and out["total_pkr"] > 50_000
    assert "deg" in out["notes"]


async def test_the_endpoints_answer_without_a_key():
    from haazir.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/ghar/meal-plan", json={"people": 5, "days": 7})
        assert r.status_code == 200 and len(r.json()["days"]) == 7
        r = await c.post("/v1/ghar/dawat", json={"guests": 50, "menu": ["biryani"]})
        assert r.status_code == 200 and r.json()["items"]
