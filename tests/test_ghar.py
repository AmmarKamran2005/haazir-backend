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
    assert units["Naan"] == "pieces" and out["total_pkr"] > 35_000
    assert "deg" in out["notes"]


async def test_the_endpoints_answer_without_a_key():
    from haazir.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/ghar/meal-plan", json={"people": 5, "days": 7})
        assert r.status_code == 200 and len(r.json()["days"]) == 7
        r = await c.post("/v1/ghar/dawat", json={"guests": 50, "menu": ["biryani"]})
        assert r.status_code == 200 and r.json()["items"]


# --- what the user asked for comes back --------------------------------------

from haazir.routers.ghar import DawatDish, align_dawat, complete_grocery  # noqa: E402


def test_custom_dishes_come_back_as_themselves_and_nothing_is_added():
    out = fallback_dawat(DawatIn(guests=50, menu=["Rabri", "Mutton biryani", "Pineapple trifle"]))
    assert [i["dish"] for i in out["items"]] == ["Rabri", "Mutton biryani", "Pineapple trifle"]
    assert all(i["est_pkr"] > 0 for i in out["items"])


def test_protein_changes_the_cost():
    def cost(protein):
        out = fallback_dawat(DawatIn(guests=60, menu=[DawatDish(dish="Biryani", protein=protein)]))
        return out["items"][0]["est_pkr"], out["items"][0]["protein"]

    assert cost("mutton")[0] > cost("beef")[0] > cost("chicken")[0]
    assert cost("mutton")[1] == "mutton"


def test_style_changes_portions():
    menu = ["Chicken biryani", "Chicken tikka"]
    desi = {i["dish"]: i["qty"] for i in fallback_dawat(DawatIn(guests=60, menu=menu))["items"]}
    bbq = {i["dish"]: i["qty"]
           for i in fallback_dawat(DawatIn(guests=60, menu=menu, style="bbq"))["items"]}
    assert bbq["Chicken tikka"] > desi["Chicken tikka"]
    assert bbq["Chicken biryani"] < desi["Chicken biryani"]


def test_a_model_that_adds_kheer_is_corrected():
    body = DawatIn(guests=40, menu=["Rabri", DawatDish(dish="Biryani", protein="mutton")])
    model = {"items": [{"dish": "Kheer", "qty": 5, "unit": "kg", "est_pkr": 3000},
                       {"dish": "Mutton Biryani", "qty": 8, "unit": "kg", "est_pkr": 40000}],
             "notes": "x"}
    out = align_dawat(model, body)
    assert [i["dish"] for i in out["items"]] == ["Rabri", "Biryani"]
    assert out["items"][1]["est_pkr"] == 40000 and out["items"][1]["protein"] == "mutton"
    assert out["total_pkr"] == sum(i["est_pkr"] for i in out["items"])


def test_breakfast_is_planned_when_asked_and_staples_are_on_the_list():
    body = MealPlanIn(people=4, days=7, meals=["breakfast", "lunch", "dinner"],
                      pantry=[PantryItem(item="sugar", qty=100, unit="kg")])
    out = fallback_plan(body, dt.date(2026, 10, 25))
    assert all(d["breakfast"] and d["lunch"] and d["dinner"] for d in out["days"])
    items = {g["item"] for g in out["grocery"]}
    assert {"Tea leaves", "Milk", "Salt", "Oil", "Atta", "Rice", "Onion", "Eggs"} <= items
    assert "Sugar" not in items  # the pantry covers it


def test_lunch_only_still_has_the_old_keys():
    out = fallback_plan(MealPlanIn(people=2, days=7, meals=["lunch"]), dt.date(2026, 10, 25))
    assert all(d["lunch"] and d["dinner"] is None and "breakfast" not in d for d in out["days"])


def test_a_model_plan_gets_its_missing_staples():
    body = MealPlanIn(people=5, days=7)
    model = {"days": [{"date": "2026-10-23", "lunch": "Daal", "dinner": "Karahi"}] * 7,
             "grocery": [{"item": "Chicken", "qty": 3, "unit": "kg", "est_pkr": 2250,
                          "when": "Week 1"}]}
    items = {g["item"] for g in complete_grocery(model, body)["grocery"]}
    assert {"Chicken", "Sugar", "Tea leaves", "Salt"} <= items
