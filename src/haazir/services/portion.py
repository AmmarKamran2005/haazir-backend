"""The serving a price buys, as a diner would say it: "1 kg", "half", "1 pound", "6 pcs".

A price without its portion is not comparable: Rs 1,800 for a kilo of karahi and Rs 950 for
a half are the same deal. The owner's own label wins; then the coarse `price_unit` the ingest
recorded; then whatever the printed name or description says.
"""

from __future__ import annotations

import re

UNIT_LABELS = {
    "per_kg": "1 kg", "half": "half", "full": "full", "per_person": "per person",
    "per_piece": "per piece",
}

_PATTERNS: list[tuple[re.Pattern, str | None]] = [
    (re.compile(r"\bserves?\s*(\d{1,2})\b"), "serves {0}"),
    (re.compile(r"\bfor\s*(\d{1,2})\s*(?:persons?|people|pax)\b"), "serves {0}"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:kg|kilo|kgs)\b"), "{0} kg"),
    (re.compile(r"\b(\d{2,4})\s*(?:g|gm|gms|grams?)\b"), "{0} g"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:lb|lbs|pounds?)\b"), "{0} pound"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:ltr|litre|liter|l)\b"), "{0} litre"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:ml)\b"), "{0} ml"),
    (re.compile(r"\b(\d{1,2})\s*(?:pcs|pc|pieces?)\b"), "{0} pcs"),
    (re.compile(r"\b(\d{1,2})\s*(?:inch|in|\")\b"), "{0} inch"),
    (re.compile(r"\bhalf\s*(?:kg|kilo)\b"), "half kg"),
    (re.compile(r"\bquarter\b"), "quarter"),
    (re.compile(r"\bhalf\b"), "half"),
    (re.compile(r"\bfull\b"), "full"),
    (re.compile(r"\bdouble\b"), "double"),
    (re.compile(r"\bsingle\b"), "single"),
    (re.compile(r"\b(small|regular|medium|large|family)\b"), "{0}"),
    (re.compile(r"\bper\s*piece\b"), "per piece"),
    (re.compile(r"\b(?:one|1)\s*pound\b"), "1 pound"),
]


def from_text(*texts: str | None) -> str | None:
    for raw in texts:
        if not raw:
            continue
        s = raw.lower()
        for pattern, label in _PATTERNS:
            m = pattern.search(s)
            if m:
                value = m.group(1) if m.groups() else ""
                if value.endswith(".0"):
                    value = value[:-2]
                return label.format(value) if label else value
    return None


def label(portion: str | None, price_unit: str | None, name: str | None,
          description: str | None = None) -> str | None:
    """The portion to show beside a price, or None when nothing says."""
    if portion and portion.strip():
        return portion.strip()
    parsed = from_text(name, description)
    if parsed:
        return parsed
    return UNIT_LABELS.get(price_unit or "")


def strip_serving(name: str) -> str:
    """The name without its serving words, for finding the dish family: "Lotus cake 1 pound"
    is a lotus cake. Falls back to the name when stripping would leave nothing."""
    s = name.lower()
    for pattern, _label in _PATTERNS:
        s = pattern.sub(" ", s)
    s = re.sub(r"[()\[\]]", " ", s)
    s = " ".join(s.split())
    return s or name
