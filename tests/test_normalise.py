"""Dish and phone canonicalisation. No database.

`docs/SCRAPING-PROMPT.md` §5 opens with "get this right or a whole feature silently dies", and
that is the reason this file is long. Every failure mode here is quiet: nothing raises, the
comparison endpoint just returns fewer rows than it should and nobody finds out. The cases
below are real strings from `scraper/out/menu_items.jsonl`, not invented ones.
"""

from __future__ import annotations

import pytest

from haazir.services.normalise import (
    _token_spellings,
    canonicalise_dish,
    normalise_phone,
    slugify,
)


def fam(name: str) -> str:
    d = canonicalise_dish(name)
    assert d is not None, name
    return d.family


def norm(name: str) -> str:
    d = canonicalise_dish(name)
    assert d is not None, name
    return d.normalized


# --- the join key ------------------------------------------------------------


@pytest.mark.parametrize(
    "printed",
    [
        "Bihari Boti", "Behari boti", "Bihari-Boti", "BIHARI BOTI",
        "Beef Bihari Boti", "Beef Behari Boti", "Chicken Behari Boti",
        "Boneless Chicken Bihari Boti", "Bihari Kabab", "Special Bihari Boti",
    ],
)
def test_every_printed_spelling_lands_in_one_family(printed):
    """The whole feature is this assertion. If these do not collapse, `/dishes/{family}/prices`
    returns a fraction of the venues that actually sell the dish and reports a median of it."""
    assert fam(printed) == "bihari boti"


def test_identity_keeps_the_protein_so_one_venue_can_sell_both():
    """`venue_dish` is keyed (venue_id, dish_id). If beef and chicken shared a dish row, a
    venue selling both would lose one to the upsert."""
    assert norm("Beef Bihari Boti") != norm("Chicken Bihari Boti")
    assert fam("Beef Bihari Boti") == fam("Chicken Bihari Boti")
    assert canonicalise_dish("Beef Bihari Boti").protein == "beef"
    assert canonicalise_dish("Chicken Bihari Boti").protein == "chicken"
    assert canonicalise_dish("Bihari Boti").protein is None


def test_a_family_never_contains_its_own_protein():
    """An alias entry can name a protein ("chicken karahi" is canonical in the map). Without
    stripping it from the resolved family, the protein gets prefixed twice."""
    assert norm("Chicken Karhai") == "chicken karahi"
    assert fam("Chicken Karhai") == "karahi"
    assert fam("Mutton Karahi") == "karahi"


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ("Nehari", "nihari"),
        ("Special Nihari", "nihari"),
        ("Chicken Briyani", "biryani"),
        ("Chicken Biriyani", "biryani"),
        ("Malai Tikka Boti", "malai boti"),
        ("Seekh Kebab", "seekh kabab"),
        ("Chappal Kabab", "chapli kabab"),
        ("Halwa Poori", "halwa puri"),
    ],
)
def test_transliteration_variants_collapse(printed, expected):
    assert fam(printed) == expected


def test_spelling_is_normalised_inside_compound_names():
    """The alias map matches whole names. Without token-level rules derived from it,
    "Behari Boti Roll" and "Bihari Boti Roll" are two families for one dish."""
    assert fam("Behari Boti Roll") == fam("Bihari Boti Roll")
    assert fam("Behari Boti Pulao") == fam("Bihari Boti Pulao")


# --- the guards on the derived spelling map ----------------------------------


def test_a_one_token_alias_does_not_become_a_word_rule():
    """`"bihari boti" <- "bihari kabab"` is true of those dishes and false of the words. As a
    token rule it says kabab -> boti, which turns Seekh Kabab into Seekh Boti."""
    rules = _token_spellings()
    assert rules.get("kabab") != "boti"
    assert rules.get("tikka") != "boti"
    assert fam("Seekh Kabab") == "seekh kabab"
    assert "boti" not in fam("Seekh Kabab")


def test_the_spelling_map_is_single_step():
    """`clean()` applies it once, so a cycle would make the output depend on dict order."""
    rules = _token_spellings()
    chained = {w: r for w, r in rules.items() if r in rules}
    assert chained == {}, f"these rules do not terminate in one pass: {chained}"


def test_no_rule_maps_a_word_to_itself():
    assert [w for w, r in _token_spellings().items() if w == r] == []


# --- portions, packs and marketing -------------------------------------------


def test_portion_words_become_a_price_unit_not_part_of_the_name():
    """A half plate and a full plate are one dish at two prices, not two dishes."""
    half = canonicalise_dish("Half Beef Karahi")
    full = canonicalise_dish("Full Beef Karahi")
    assert half.family == full.family == "karahi"
    assert half.price_unit == "half"
    assert full.price_unit == "full"
    assert canonicalise_dish("Seekh Kabab per kg").price_unit == "per_kg"


def test_pack_sizes_are_stripped():
    """Otherwise "Almond Biscotti 210gm" and "Almond Biscotti 330gm" are two families that can
    never be compared with each other or with anyone else's."""
    assert fam("Almond Biscotti 210gm") == fam("Almond Biscotti 330gm") == "almond biscotti"
    assert fam("Mineral Water 500ml") == "mineral water"


def test_marketing_words_are_dropped():
    assert fam("Chef's Special Famous Nihari") == "nihari"


def test_nothing_usable_returns_none_rather_than_an_empty_dish():
    assert canonicalise_dish("") is None
    assert canonicalise_dish("   ") is None
    assert canonicalise_dish("!!!") is None


# --- singularisation is deliberately timid -----------------------------------


def test_singularise_never_invents_a_stem():
    """A general stemmer turns "rice" into "ric" and "fries" into "frie", manufacturing join
    keys that match nothing and are invisible until a comparison returns one row."""
    assert fam("Rice") == "rice"
    assert fam("French Fries") == "french fries"
    assert fam("Samosas") == "samosa"  # this one IS in the alias map


# --- phone -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+923003688929", "+923003688929"),      # mobile
        ("0300 368 8929", "+923003688929"),      # mobile, trunk prefix and spaces
        ("00923003688929", "+923003688929"),     # 00 country prefix
        ("+9221111529233", "+9221111529233"),    # UAN
        ("021-111-529-233", "+9221111529233"),   # UAN, national format
        ("0213 5870000", "+922135870000"),       # Karachi landline
        ("(021) 3587 0000", "+922135870000"),
    ],
)
def test_phone_to_e164(raw, expected):
    assert normalise_phone(raw) == expected


def test_uan_numbers_survive():
    """An earlier version accepted only one national-number length and silently dropped 257
    real UANs, which shows up as an empty call button months later, never as an error."""
    assert normalise_phone("+9221111529233") == "+9221111529233"


@pytest.mark.parametrize("raw", [None, "", "garbage", "12", "1" * 20])
def test_unrecognisable_numbers_are_none_not_a_guess(raw):
    assert normalise_phone(raw) is None


# --- slug --------------------------------------------------------------------


def test_slug_is_url_safe_and_suffixed():
    assert slugify("Kolachi Restaurant!", "abc123") == "kolachi-restaurant-abc123"
    assert slugify("Café  Piyala") == "cafe-piyala"
