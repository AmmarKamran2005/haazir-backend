"""Portions, the model chain, and chat follow-ups: the pieces that need no database."""

from __future__ import annotations

import time

from haazir.services import chat, llm, portion


def test_portions_are_read_from_what_the_menu_says():
    assert portion.label(None, "per_plate", "Lotus cake 1 pound") == "1 pound"
    assert portion.label(None, "per_plate", "Chicken Karahi Half") == "half"
    assert portion.label(None, "per_plate", "Mutton karahi (1 kg)") == "1 kg"
    assert portion.label(None, "per_plate", "Seekh kabab 6 pcs") == "6 pcs"
    assert portion.label(None, "per_plate", "Family platter", "Serves 4") == "family"
    assert portion.label(None, "per_kg", "Mutton karahi") == "1 kg"
    assert portion.label("1 pound", "per_plate", "Lotus cake") == "1 pound"
    assert portion.label(None, "per_plate", "Chicken biryani") is None


def test_the_chain_skips_a_benched_model(monkeypatch):
    monkeypatch.setattr(llm, "MODEL_CHAIN", ["a", "b", "c"])
    monkeypatch.setattr(llm, "_benched", {"a": time.monotonic() + 60})
    assert llm._order(None) == ["b", "c"]
    assert llm._order("c") == ["c", "b"]
    monkeypatch.setattr(llm, "_benched", {m: time.monotonic() + 60 for m in "abc"})
    assert llm._order(None) == ["a", "b", "c"]  # all benched: try them anyway


def test_thought_signatures_are_stripped_for_another_model():
    body = {"contents": [{"role": "model", "parts": [{"functionCall": {}, "thoughtSignature": "x"}]}]}
    assert "thoughtSignature" not in llm._without_signatures(body)["contents"][0]["parts"][0]


def test_no_zero_minute_wait():
    assert chat.wait_phrase(0, roman=False) == "no wait right now"
    assert chat.wait_phrase(0.4, roman=True) == "abhi koi wait nahi"
    assert chat.wait_phrase(18, roman=False) == "about 18 min wait"


def test_follow_ups_are_recognised():
    assert chat.refers_back("Yahan ka menu btao")
    assert chat.refers_back("is restaurant mein family hall hai?")
    assert not chat.refers_back("Bahadurabad mein biryani")
    assert chat.is_menu_question("Yahan ka menu btao")


def test_the_menu_template_quotes_price_and_portion():
    text = chat.menu_reply("China Town", [
        {"name": "Chicken Manchurian", "price_pkr": 1250, "portion": "half"},
        {"name": "Egg fried rice", "price_pkr": 800, "portion": None},
    ], roman=True)
    assert "Chicken Manchurian Rs 1,250 (half)" in text and "Egg fried rice Rs 800" in text
    assert "nahi hai" in chat.menu_reply("China Town", [], roman=True)
