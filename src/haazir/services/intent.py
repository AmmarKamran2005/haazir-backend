"""Roman-Urdu intent parsing. Port of `parseQuery` in `app/assets/js/app.js`. Plan §9.

*"The deterministic Roman-Urdu parser must be ported as-is. It is the offline fallback and the
audit trail."*

Both halves of that matter. It is the **fallback** because Phase 8's acceptance criterion is
that removing the LLM key leaves the product working end to end, and this is what keeps that
true: roughly 85% of real queries are handled here without a model call. It is the **audit
trail** because `extracted` records what the system believed the user said, in the user's own
terms, so a wrong result can be traced to a misreading rather than to an unexplainable model.

Two behaviours are easy to lose and were both bugs once.

**Party size is read before budget.** "2500 tak, 6 log hain" is two and a half thousand for
the whole table, not each. Read the other way round, the app quietly sextuples the budget and
returns restaurants nobody can afford.

**"abhi" means *now*, not *hurry*.** Treating it as travel urgency capped
"nihari, abhi sahi hai ya subah aaun?" at fifteen minutes and dropped half the nihari in the
city from a question that was about timing, not distance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A default only when the user gave no number at all. Never used to override one they did.
DEFAULT_BUDGET_PKR = 1500
DEFAULT_MAX_TRAVEL_MIN = 30

PARTY_RE = re.compile(r"(\d+)\s*(log|bande|banday|people|person|ppl|guests|jane|afraad)")
SOLO_RE = re.compile(r"\b(akela|akeli|alone|solo|khud|by myself)\b")
MONEY_RE = re.compile(r"(\d{3,6})\s*(rs|rupees|rupay|tak|k\b)?")
PER_HEAD_RE = re.compile(r"per head|per person|har banday|har bande|each|fi kas")
TRAVEL_RE = re.compile(r"(\d{1,2})\s*(min|minute|mint|minat)")

# "jaldi" is haste, and haste is about distance. "abhi" is about the clock and belongs
# nowhere near a travel limit. See the module note.
HURRY_RE = re.compile(r"\b(jaldi|jldi|fast|quick|hurry|nazdeek|paas mein)\b")

MOODS: list[tuple[str, re.Pattern]] = [
    ("spicy", re.compile(r"spicy|teekha|tikha|teekhi|masaledar|chatpata|mirch")),
    ("bbq", re.compile(r"bbq|barbeque|barbecue|tikka|kabab|kebab|boti|karahi|grill|seekh")),
    ("quiet", re.compile(r"quiet|sukoon|peaceful|baat|talk|calm|shor nahi|khamosh")),
    ("seafood", re.compile(r"seafood|fish|machli|jhinga|prawn|crab")),
]

CONSTRAINTS: list[tuple[str, str, re.Pattern]] = [
    ("needs_family", "Family section",
     re.compile(r"family|ghar wal|ghar walo|bachay|bache|bachon|ammi|abbu|kids|children")),
    ("needs_prayer", "Prayer area", re.compile(r"prayer|namaz|masjid|salah|jamaat")),
    ("needs_ramp", "Step-free access",
     re.compile(r"wheelchair|ramp|step-free|step free|walker|whel chair")),
    ("needs_card", "Card accepted",
     re.compile(r"\bcard\b|debit|credit|cash nahi|cash nai")),
]

DIET: list[tuple[str, str, re.Pattern]] = [
    ("nut_allergy", "Nut allergy",
     re.compile(r"nut allergy|nuts allergy|allergy.*nut|badam se allergy|peanut")),
    ("no_beef", "No beef", re.compile(r"no beef|beef nahi|beef nai|bina beef|gaay ka nahi")),
    ("vegetarian", "Vegetarian",
     re.compile(r"vegetarian|veggie|\bveg\b|sabzi|bina gosht|meat nahi")),
    ("halal", "Halal", re.compile(r"halal|zabiha")),
]

CHEAP_RE = re.compile(r"sasta|sasti|cheap|budget mein|kam paison|kam paise|affordable")
CHEAP_BUDGET_PKR = 700

LATE_RE = re.compile(r"\blate\b|raat|night|11 baje|12 baje|midnight|der se")

# Dish families the parser recognises by name. Deliberately the printed spellings people
# actually type; `services.normalise` canonicalises whatever comes out.
DISHES = (
    "nihari", "nehari", "biryani", "briyani", "karahi", "karhai", "haleem", "broast",
    "burger", "mandi", "rabri", "halwa puri", "brownie", "tikka", "kabab", "kebab",
    "paye", "payay", "sushi", "pizza", "falooda", "bihari boti", "behari boti",
    "malai boti", "chaat", "samosa", "pulao", "chapli", "sajji", "steak", "pasta",
)


@dataclass(frozen=True, slots=True)
class Extraction:
    """One thing the parser believed it understood, in the user's own words.

    This is the audit trail: a wrong result should be traceable to a misreading rather than
    to a model nobody can question.
    """

    key: str
    value: str
    source: str = "parser"

    def as_dict(self) -> dict:
        return {"key": self.key, "value": self.value, "source": self.source}


@dataclass(slots=True)
class Intent:
    raw: str
    party: int = 1
    budget_pkr: int | None = None
    budget_total_pkr: int | None = None
    max_travel_min: int = DEFAULT_MAX_TRAVEL_MIN
    mood: str | None = None
    dish: str | None = None
    diet: list[str] = field(default_factory=list)
    needs_family: bool = False
    needs_prayer: bool = False
    needs_ramp: bool = False
    needs_card: bool = False
    late: bool = False
    extracted: list[Extraction] = field(default_factory=list)
    # False when the parser found nothing at all, which is the one case worth asking a model
    # about. Everything else is handled here for free.
    understood: bool = True

    def as_dict(self) -> dict:
        return {
            "raw": self.raw,
            "party": self.party,
            "budget_pkr": self.budget_pkr,
            "budget_total_pkr": self.budget_total_pkr,
            "max_travel_min": self.max_travel_min,
            "mood": self.mood,
            "dish": self.dish,
            "diet": self.diet,
            "needs_family": self.needs_family,
            "needs_prayer": self.needs_prayer,
            "needs_ramp": self.needs_ramp,
            "needs_card": self.needs_card,
            "late": self.late,
            "understood": self.understood,
            "extracted": [e.as_dict() for e in self.extracted],
        }


def _rs(amount: int) -> str:
    return f"Rs {amount:,}"


def parse(text: str) -> Intent:
    """Roman Urdu or English in, a structured query out. Never raises."""
    raw = (text or "").strip()
    padded = f" {raw.lower()} "
    intent = Intent(raw=raw)

    # --- party size, BEFORE budget. See the module note. ---------------------
    party_match = PARTY_RE.search(padded)
    if party_match:
        intent.party = max(1, min(30, int(party_match.group(1))))
        intent.extracted.append(Extraction("Party size", f"{intent.party} people"))
    elif SOLO_RE.search(padded):
        intent.party = 1
        intent.extracted.append(Extraction("Party size", "Solo"))

    # --- budget --------------------------------------------------------------
    money_match = MONEY_RE.search(padded)
    if money_match:
        amount = int(money_match.group(1))
        if re.search(r"\bk\b", money_match.group(0)) and amount < 100:
            amount *= 1000
        # Under Rs 200 is far more likely a time, a table number or a street address than a
        # dining budget for anybody.
        if amount >= 200:
            per_head = bool(PER_HEAD_RE.search(padded))
            intent.budget_total_pkr = amount * intent.party if per_head else amount
            intent.budget_pkr = round(intent.budget_total_pkr / intent.party)
            intent.extracted.append(
                Extraction(
                    "Budget",
                    f"{_rs(intent.budget_total_pkr)} total · {_rs(intent.budget_pkr)} a head"
                    if intent.party > 1
                    else _rs(intent.budget_pkr),
                )
            )

    # --- mood ----------------------------------------------------------------
    for name, pattern in MOODS:
        if pattern.search(padded):
            intent.mood = name
            intent.extracted.append(Extraction("Mood", name.replace("bbq", "BBQ / grill")))
            break

    # --- hard constraints ----------------------------------------------------
    for attribute, label, pattern in CONSTRAINTS:
        if pattern.search(padded):
            setattr(intent, attribute, True)
            intent.extracted.append(Extraction("Hard constraint", label))

    for key, label, pattern in DIET:
        if pattern.search(padded):
            intent.diet.append(key)
            intent.extracted.append(Extraction("Hard constraint", label))

    # --- travel --------------------------------------------------------------
    travel_match = TRAVEL_RE.search(padded)
    if travel_match:
        intent.max_travel_min = max(5, min(120, int(travel_match.group(1))))
        intent.extracted.append(Extraction("Max travel", f"{intent.max_travel_min} min"))
    elif HURRY_RE.search(padded):
        intent.max_travel_min = 15
        intent.extracted.append(
            Extraction("Max travel", '15 min (inferred from "jaldi")')
        )

    # --- inferred budget -----------------------------------------------------
    if CHEAP_RE.search(padded) and intent.budget_pkr is None:
        intent.budget_pkr = CHEAP_BUDGET_PKR
        intent.budget_total_pkr = CHEAP_BUDGET_PKR * intent.party
        intent.extracted.append(
            Extraction("Budget", f'{_rs(CHEAP_BUDGET_PKR)} a head (inferred from "sasta")')
        )

    if LATE_RE.search(padded):
        intent.late = True
        intent.extracted.append(Extraction("Time", "Late night"))

    # --- dish ----------------------------------------------------------------
    for dish in DISHES:
        if dish in padded:
            intent.dish = dish
            intent.extracted.append(Extraction("Dish", dish.title()))
            break

    if intent.budget_pkr is None:
        intent.budget_pkr = DEFAULT_BUDGET_PKR
        intent.budget_total_pkr = DEFAULT_BUDGET_PKR * intent.party

    # Nothing recognised at all. The one case where asking a model is worth the money and the
    # latency; everything above was free.
    intent.understood = bool(intent.extracted)
    return intent


def to_search_query(intent: Intent, **overrides):
    """An `Intent` as the scoring layer's `Query`."""
    from ..estimator.scoring import Query
    from .normalise import canonicalise_dish

    dish_family = None
    if intent.dish:
        parsed = canonicalise_dish(intent.dish)
        dish_family = parsed.family if parsed else None

    fields = {
        "text": intent.raw,
        "party": intent.party,
        "budget": intent.budget_pkr,
        "max_travel": intent.max_travel_min,
        "mood": intent.mood,
        "dish": dish_family,
        "diet": intent.diet,
        "needs_family": intent.needs_family,
        "needs_prayer": intent.needs_prayer,
        "needs_ramp": intent.needs_ramp,
        "needs_card": intent.needs_card,
    }
    return Query(**(fields | overrides))
