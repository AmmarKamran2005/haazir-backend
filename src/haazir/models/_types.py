"""Shared column types and mixins.

The Postgres enums are declared with `create_type=False` throughout. The migrations own their
lifecycle; if SQLAlchemy also tried to create them, every `CREATE TABLE` in a fresh database
would race the migration that already made the type and fail with `DuplicateObject`.
"""

from __future__ import annotations

import datetime as dt
import uuid

from geoalchemy2 import Geography
from sqlalchemy import DateTime, func, text
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column


def pg_enum(name: str, *values: str) -> ENUM:
    return ENUM(*values, name=name, create_type=False)


UserRole = pg_enum("user_role", "diner", "owner", "admin")
VenueTier = pg_enum("venue_tier", "seeded", "claimed", "live")
VenueStatus = pg_enum(
    "venue_status", "active", "temporarily_closed", "permanently_closed", "hidden"
)
ObsSource = pg_enum("obs_source", "staff", "checkin", "payment", "prior", "pos")
RegEventType = pg_enum("reg_event_type", "sealed", "fined", "notice", "cleared", "reopened")
OfferStatus = pg_enum("offer_status", "draft", "live", "paused", "expired")
PriceUnit = pg_enum(
    "price_unit", "per_plate", "per_kg", "half", "full", "per_person", "per_piece"
)

Point = Geography(geometry_type="POINT", srid=4326, spatial_index=False)
Polygon = Geography(geometry_type="POLYGON", srid=4326, spatial_index=False)

TZ = DateTime(timezone=True)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def created_at() -> Mapped[dt.datetime]:
    return mapped_column(TZ, nullable=False, server_default=func.now())
