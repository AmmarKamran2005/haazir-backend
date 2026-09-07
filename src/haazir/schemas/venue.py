"""Response models for the discovery endpoints.

Two shapes recur and both are deliberate.

An access fact is never a bare boolean on the wire. `{"v": true, "c": 0.75, "n": 0}` says
what we believe, how sure we are, and how many people checked. Flattening it to `true` would
throw away the only part a diner in a wheelchair actually needs.

Anything derived carries where it came from. `OccupancySummary.source` says whether a number
is a measurement or a category archetype, because a prior dressed up as an observation is the
failure this product exists to avoid.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field


class AreaRef(BaseModel):
    id: int
    name: str
    name_urdu: str | None = None


class OccupancySummary(BaseModel):
    occupancy: float = Field(description="0..1")
    sd: float
    confidence: float
    band: str
    wait_p50_min: float | None = None
    wait_p90_min: float | None = None
    source_weights: dict = Field(default_factory=dict)
    updated_at: dt.datetime | None = None
    # "live" once observations exist, "prior" until then. Never omitted.
    source: str = "prior"


class TrustComponent(BaseModel):
    """One row of the trust breakdown. A list, not a dict, because the order is the order
    the venue page renders them in and a dict would not preserve it (see estimator/trust.py)."""

    key: str
    pts: int
    max: int
    note: str | None = None


class VenueCard(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    name_urdu: str | None = None
    brand: str | None = None
    branch_label: str | None = None
    area: AreaRef | None = None
    lat: float
    lng: float
    address_full: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    website: str | None = None
    instagram: str | None = None
    maps_url: str | None = None
    venue_type: str
    cuisines: list[str] = Field(default_factory=list)
    price_level: int | None = None
    avg_ticket_pkr: int | None = None
    capacity_covers: int | None = None
    google_rating: float | None = None
    google_review_count: int | None = None
    hours: dict | None = None
    hours_ramadan: dict | None = None
    attributes: dict = Field(default_factory=dict)
    photos: list = Field(default_factory=list)
    blurb: str | None = None
    tier: str
    status: str
    trust_score: int | None = None
    trust_components: list[TrustComponent] | None = None
    live: OccupancySummary | None = None


class DishLine(BaseModel):
    dish_id: uuid.UUID
    name: str = Field(description="canonical dish name")
    menu_name: str = Field(description="as printed on the menu")
    family: str
    protein: str | None = None
    section: str | None = None
    description: str | None = None
    price_pkr: int | None = None
    price_unit: str
    price_seen_at: dt.datetime | None = None
    price_age_days: int | None = None
    is_signature: bool = False
    sold_out_until: dt.datetime | None = None
    quality_mean: float | None = None


class VenueDishes(BaseModel):
    venue_id: uuid.UUID
    count: int
    dishes: list[DishLine]
    # True when no dish here has a price. The UI must say "menu not priced yet" rather than
    # render an empty table that looks like a bug.
    unpriced: bool = False


class PriceQuote(BaseModel):
    venue_id: uuid.UUID
    slug: str
    venue_name: str
    area: str | None = None
    menu_name: str
    protein: str | None = None
    price_pkr: int
    price_unit: str
    price_seen_at: dt.datetime | None = None
    price_age_days: int | None = None
    stale: bool = Field(
        default=False,
        description="Older than the staleness threshold. Shown, never silently dropped.",
    )
    distance_m: float | None = None


class PriceComparison(BaseModel):
    family: str
    protein: str | None = None
    venue_count: int
    median_pkr: int | None = None
    min_pkr: int | None = None
    max_pkr: int | None = None
    stale_after_days: int
    quotes: list[PriceQuote]
    note: str | None = None


class AreaPulse(BaseModel):
    area_id: int
    name: str
    name_urdu: str | None = None
    lat: float
    lng: float
    venue_count: int
    occupancy_mean: float
    band: str
    live_venue_count: int = Field(
        description="How many of these are backed by an observation rather than a prior"
    )


class CityPulse(BaseModel):
    city: str
    at: dt.datetime
    hour_of_week: int
    areas: list[AreaPulse]
    # Says plainly how much of this map is measured and how much is modelled.
    live_fraction: float


class CityStats(BaseModel):
    city: str
    at: dt.datetime
    venue_count: int
    utilisation_now: float
    weekly_mean_utilisation: float
    idle_seats_now: int | None = None
    busiest_area: str | None = None
    quietest_area: str | None = None
    live_fraction: float
