"""Dish and phone canonicalisation. Plan §10.1 rule 3, `docs/SCRAPING-PROMPT.md` §5.

`dish.name_normalized` is the join key for cross-venue price comparison, and the plan is blunt
about the failure mode: if the variants do not collapse, the feature returns nothing and
nobody finds out why. So this module is tested against real menu strings, not invented ones.

**The protein problem, and why `family` exists.** The spec's example map says
`nihari <- ... beef nihari, maghaz nihari`, i.e. protein qualifiers collapse into the base
dish. Taken literally that breaks two things at once. `venue_dish` is keyed
`(venue_id, dish_id)`, so a venue selling both Beef Bihari Boti and Chicken Bihari Boti would
collide on insert. And comparing a beef price against a chicken price across venues is a
wrong answer presented confidently, which is the one thing this product exists not to do.

So a dish keeps its full identity in `name_normalized` ("beef bihari boti") and also carries
`family` ("bihari boti") and `protein` ("beef"). The comparison endpoint groups by `family`
and can narrow by `protein`. The join the spec asked for exists; the misleading comparison
does not.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ALIAS_FILE = DATA_DIR / "dish_aliases.json"

# Words that describe how a restaurant feels about its own food, not what the food is.
MARKETING = (
    "special", "famous", "desi", "original", "house", "chef", "chefs", "signature",
    "best", "delicious", "super", "premium", "authentic", "traditional", "classic",
    "our", "new", "hot", "fresh",
)

# Portion words leave the name and become `price_unit`, so that a half plate and a full plate
# are the same dish at two prices rather than two dishes.
PORTION_UNITS: dict[str, str] = {
    "half": "half",
    "full": "full",
    "per kg": "per_kg",
    "kg": "per_kg",
    "per person": "per_person",
    "per head": "per_person",
    "piece": "per_piece",
    "pieces": "per_piece",
    "pc": "per_piece",
    "pcs": "per_piece",
    "plate": "per_plate",
}
# Sizes that are not portions of a standard unit, just adjectives. Dropped, not recorded.
SIZE_WORDS = ("large", "small", "regular", "medium", "jumbo", "family", "single", "double", "mini")

PROTEINS: dict[str, str] = {
    "beef": "beef", "mutton": "mutton", "lamb": "mutton", "goat": "mutton",
    "chicken": "chicken", "murgh": "chicken", "fish": "fish", "prawn": "prawn",
    "prawns": "prawn", "shrimp": "prawn", "veg": "vegetarian",
    "vegetable": "vegetarian", "vegetarian": "vegetarian", "paneer": "vegetarian",
}
# Cut and preparation qualifiers that sit alongside a protein and are not the dish either.
CUT_WORDS = ("boneless", "bone in", "bonein", "with bone", "undercut", "tikka boti")


@dataclass(frozen=True, slots=True)
class DishName:
    """The result of canonicalising one printed menu line."""

    canonical: str  # "Beef Bihari Boti" — title case, for display
    normalized: str  # "beef bihari boti" — UNIQUE per dish row, the identity
    family: str  # "bihari boti" — the comparison join key
    protein: str | None  # "beef"
    price_unit: str | None  # "half" | "per_kg" | ... , None means per_plate


@lru_cache(maxsize=1)
def _alias_map() -> dict[str, str]:
    """`variant -> canonical family`, flattened from the data file.

    Kept as data rather than code so a new spelling seen in production is a one-line edit
    with no deploy of logic. Seeded from `scraper/data/dish_aliases.json`; the two are
    allowed to drift, because the backend is the side that has to be right.
    """
    if not ALIAS_FILE.exists():
        return {}
    raw = json.loads(ALIAS_FILE.read_text(encoding="utf-8"))
    raw.pop("_readme", None)
    out: dict[str, str] = {}
    for canonical, variants in raw.items():
        out[canonical] = canonical
        for v in variants:
            out[v] = canonical
    return out


def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def _words(s: str) -> list[str]:
    return [w for w in s.split() if w]


# Pack sizes. "Almond Biscotti 210gm" and "Almond Biscotti 330gm" are the same dish sold in
# two boxes, and leaving the weight in the name makes them two families that can never be
# compared with each other or with anybody else's.
_PACK_SIZE = re.compile(r"\b\d+\s*(?:gm|gms|g|kgs?|ml|ltrs?|litres?|l|oz|lbs?)\b")


@lru_cache(maxsize=1)
def _token_spellings() -> dict[str, str]:
    """Single-word spelling fixes, derived from the alias file rather than hand-listed.

    The alias map matches whole names, so it fixes "behari boti" and leaves "behari boti
    roll" alone: two families that differ only in how someone transliterated the same Urdu
    word. Wherever a variant and its canonical form differ in exactly one token, that pair is
    a spelling rule that can be applied inside any compound name. Extending the data file
    therefore improves compound handling for free, with no code change.

    Two guards, and both are load-bearing.

    A one-token difference is not always a spelling difference. `"bihari boti" <- "bihari
    kabab"` is a true statement about those two whole dishes and a false one about the words:
    taken as a token rule it says `kabab -> boti`, which silently turns Seekh Kabab into
    Seekh Boti, a different dish at a different price. So a pair only becomes a rule if the
    two tokens actually look like transliterations of each other.

    And a token that is claimed by two different targets is dropped rather than resolved by
    whichever entry the file happened to list first.
    """
    from difflib import SequenceMatcher

    candidates: dict[str, set[str]] = {}
    for variant, canonical in _alias_map().items():
        v, c = variant.split(), canonical.split()
        if len(v) != len(c):
            continue
        differing = [(a, b) for a, b in zip(v, c, strict=True) if a != b]
        if len(differing) != 1:
            continue
        wrong, right = differing[0]
        # Transliteration variants of one Urdu word look alike; two different dishes do not.
        if SequenceMatcher(None, wrong, right).ratio() < 0.6:
            continue
        candidates.setdefault(wrong, set()).add(right)

    rules = {
        wrong: next(iter(rights))
        for wrong, rights in candidates.items()
        if len(rights) == 1
    }

    # Third guard: the map must be single-step. "kabab" and "kebab" each appear as the
    # canonical spelling in different entries, so the pairs derive both `kabab -> kebab` and
    # `kebab -> kabab`. `clean()` applies this map exactly once, so a cycle makes the output
    # depend on dictionary order rather than on the data. Dropping any rule whose target is
    # itself a source leaves a mapping that always reaches a fixed point in one pass; the
    # whole-name alias lookup still resolves those words correctly a step later.
    return {wrong: right for wrong, right in rules.items() if right not in rules}


def clean(raw: str) -> str:
    """Lowercase, unaccent, drop punctuation, pack sizes and marketing words."""
    s = _strip_accents(raw.lower())
    s = _PACK_SIZE.sub(" ", s)
    # Possessives before punctuation, or "chef's" becomes "chef s": the marketing word is
    # then dropped and the orphaned "s" stays behind as part of the family name.
    s = re.sub(r"[’']s\b", " ", s)
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    spellings = _token_spellings()
    s = " ".join(
        spellings.get(w, w)
        for w in _words(s)
        # A one-character token is always debris: a stray plural, an initial, an item number.
        if len(w) > 1 and w not in MARKETING and w not in SIZE_WORDS
    )
    return s.strip()


def extract_portion(s: str) -> tuple[str, str | None]:
    """Pull a portion word out of the name and return it as a `price_unit`."""
    unit: str | None = None
    # Two-word units first, so "per kg" is not seen as the bare word "kg".
    for phrase, mapped in sorted(PORTION_UNITS.items(), key=lambda kv: -len(kv[0])):
        pattern = rf"\b{re.escape(phrase)}\b"
        if re.search(pattern, s):
            unit = unit or mapped
            s = re.sub(pattern, " ", s)
    return " ".join(_words(s)), unit


def extract_protein(s: str) -> tuple[str, str | None]:
    """Pull the protein out, so `family` is the dish and the protein is a facet on it."""
    protein: str | None = None
    for word in _words(s):
        if word in PROTEINS:
            protein = PROTEINS[word]
            break
    if protein:
        s = " ".join(w for w in _words(s) if w not in PROTEINS)
    return " ".join(_words(s)), protein


def strip_cuts(s: str) -> str:
    """Remove cut and preparation qualifiers. Applied last, never before an alias lookup."""
    for phrase in CUT_WORDS:
        s = re.sub(rf"\b{re.escape(phrase)}\b", " ", s)
    return " ".join(_words(s))


def singularise(s: str) -> str:
    """Conservative, and deliberately so.

    A general stemmer turns "rice" into "ric" and "fries" into "frie", inventing join keys
    that match nothing and are invisible until the comparison silently returns one row. A
    trailing `s` is only dropped when the stem is a name the alias map already knows, so this
    can make two real dishes agree and can never manufacture a word.
    """
    aliases = _alias_map()
    out = []
    for w in _words(s):
        stem = w[:-1]
        out.append(stem if w.endswith("s") and len(w) > 3 and stem in aliases else w)
    return " ".join(out)


def canonicalise_dish(raw: str) -> DishName | None:
    """One printed menu line to a dish identity. `None` if nothing usable is left."""
    cleaned = clean(raw)
    if not cleaned:
        return None

    without_portion, unit = extract_portion(cleaned)
    aliases = _alias_map()

    # Try the alias map on the fullest string first and only strip when it misses. The map's
    # entries are written as whole printed names ("malai tikka boti"), so removing the cut
    # word "tikka boti" up front would destroy the exact string the entry exists to catch and
    # leave "malai", which is not a dish.
    if without_portion in aliases:
        family = aliases[without_portion]
        _, protein = extract_protein(without_portion)
    else:
        base, protein = extract_protein(without_portion)
        if base in aliases:
            family = aliases[base]
        else:
            base = singularise(strip_cuts(base))
            family = aliases.get(base, base)

    # An alias entry may itself name a protein ("chicken karahi" is a canonical family in the
    # map). Without this, resolving "Chicken Karhai" through that entry and then prefixing the
    # protein again yields "chicken chicken karahi". `family` is always protein-free.
    family, family_protein = extract_protein(family)
    protein = protein or family_protein

    # `normalized` keeps the protein so two dishes at one venue cannot collide on the
    # (venue_id, dish_id) primary key. `family` is what the comparison groups on.
    normalized = f"{protein} {family}".strip() if protein else family
    if not normalized:
        return None

    return DishName(
        canonical=" ".join(w.capitalize() for w in normalized.split()),
        normalized=normalized,
        family=family,
        protein=protein,
        price_unit=unit,
    )


# --- phone -------------------------------------------------------------------

# Pakistan's national significant number is not one fixed length. A mobile is 10 digits
# (3XX XXXXXXX), a Karachi landline is 9 (21 XXXXXXX), and a UAN is 11 (21 111 XXX XXX).
# An earlier version of this function accepted only one length and silently discarded 257
# real numbers, all of them UANs, which is exactly the kind of loss that shows up as an
# empty "call" button months later rather than as an error.
_PK_NSN_MIN, _PK_NSN_MAX = 9, 11


def normalise_phone(raw: str | None) -> str | None:
    """To E.164. Returns None rather than a guess when the number is not recognisable."""
    if not raw:
        return None

    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None

    # Strip the country code however it was written: +92, 0092, or a bare leading 92 on a
    # number too long to be national.
    if digits.startswith("0092"):
        nsn = digits[4:]
    elif digits.startswith("92") and len(digits) > _PK_NSN_MAX:
        nsn = digits[2:]
    else:
        nsn = digits.lstrip("0")  # national trunk prefix

    if not (_PK_NSN_MIN <= len(nsn) <= _PK_NSN_MAX):
        return None
    return f"+92{nsn}"


def slugify(name: str, suffix: str = "") -> str:
    s = _strip_accents(name.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s) or "venue"
    return f"{s}-{suffix}" if suffix else s
