"""Chat pieces that need no database: language, banks, the template, the compact row."""

from __future__ import annotations

from haazir.services import chat

ROW = {"slug": "naseeb", "name": "Naseeb Biryani", "area": "Bahadurabad",
       "live": {"band": "free", "source": "live", "occupancy": 0.31, "wait_p50_min": 2.4},
       "travel_min": 8, "expected_spend_pkr": 1400, "cuisines": ["Biryani"]}


def test_roman_urdu_is_recognised():
    assert chat.is_roman_urdu("Bahadurabad mein abhi khali biryani, 4 log")
    assert not chat.is_roman_urdu("Where can I get biryani in Clifton?")


def test_banks_are_read_out_of_the_question():
    assert chat.banks_in("Rs 1500 tak, HBL card") == ["HBL"]
    assert chat.banks_in("meezan ya ubl") == ["Meezan Bank", "UBL"]


def test_the_compact_row_has_no_internal_terms():
    row = chat.compact(ROW, [{"bank": "HBL", "discount_pct": 20}])
    assert row["status"] == "Abhi khali / free right now" and row["measured_now"] is True
    assert row["deals"] == ["HBL 20% off"]
    assert "prior" not in str(row) and "confidence" not in str(row)


def test_the_template_answers_in_the_users_language():
    rows = [chat.compact(ROW, [{"bank": "HBL", "discount_pct": 20}])]
    assert "Abhi khali" in chat.template_reply(rows, roman=True)
    assert "free right now" in chat.template_reply(rows, roman=False)
    assert chat.template_reply([], roman=True)


def test_customers_never_see_percentages_or_internal_bands():
    busy = {**ROW, "live": {"band": "moderate", "source": "live", "occupancy": 0.62,
                            "wait_p50_min": 6}}
    row = chat.compact(busy)
    text = chat.template_reply([row], roman=True) + chat.template_reply([row], roman=False)
    blob = str(row) + text
    assert "%" not in text and "62" not in blob and "moderate" not in blob.lower()
    assert "Thoda rush" in text and "a little busy" in text
    assert chat.status_phrase("full", 20) == "Full, ~20 min wait / full, ~20 min wait"
