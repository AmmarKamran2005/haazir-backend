"""Geography and venues. §3.2, §3.3, §3.4."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    REAL as Real,
)
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._types import TZ, Point, Polygon, VenueStatus, VenueTier, created_at, uuid_pk


class City(Base):
    __tablename__ = "city"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name_urdu: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str] = mapped_column(Text, nullable=False, server_default="PK")
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="Asia/Karachi")
    centroid: Mapped[Any] = mapped_column(Point, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    areas: Mapped[list[Area]] = relationship(back_populates="city")


class Area(Base):
    __tablename__ = "area"
    __table_args__ = (UniqueConstraint("city_id", "name", name="area_city_id_name_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    city_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("city.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_urdu: Mapped[str | None] = mapped_column(Text)
    centroid: Mapped[Any] = mapped_column(Point, nullable=False)
    boundary: Mapped[Any | None] = mapped_column(Polygon)

    city: Mapped[City] = relationship(back_populates="areas")


class Venue(Base):
    __tablename__ = "venue"
    __table_args__ = (
        CheckConstraint("price_level BETWEEN 1 AND 4", name="venue_price_level_check"),
        Index("venue_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
        Index("venue_cuisines", "cuisines", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    place_id: Mapped[str | None] = mapped_column(Text, unique=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_urdu: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    brand: Mapped[str | None] = mapped_column(Text)
    branch_label: Mapped[str | None] = mapped_column(Text)

    city_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("city.id"), nullable=False)
    area_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("area.id"))
    geom: Mapped[Any] = mapped_column(Point, nullable=False)
    address_full: Mapped[str | None] = mapped_column(Text)

    phone: Mapped[str | None] = mapped_column(Text)
    whatsapp: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    instagram: Mapped[str | None] = mapped_column(Text)
    maps_url: Mapped[str | None] = mapped_column(Text)

    venue_type: Mapped[str] = mapped_column(Text, nullable=False)
    cuisines: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    price_level: Mapped[int | None] = mapped_column(SmallInteger)
    avg_ticket_pkr: Mapped[int | None] = mapped_column(Integer)
    capacity_covers: Mapped[int | None] = mapped_column(Integer)

    google_rating: Mapped[float | None] = mapped_column(Real)
    google_review_count: Mapped[int | None] = mapped_column(Integer)
    rating_histogram: Mapped[dict | None] = mapped_column(JSONB)

    hours: Mapped[dict | None] = mapped_column(JSONB)
    hours_ramadan: Mapped[dict | None] = mapped_column(JSONB)
    popular_times: Mapped[dict | None] = mapped_column(JSONB)

    # §3.4. Every access fact is {v, c, n, at}, never a bare boolean.
    attributes: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    photos: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    blurb: Mapped[str | None] = mapped_column(Text)

    tier: Mapped[str] = mapped_column(VenueTier, nullable=False, server_default="seeded")
    status: Mapped[str] = mapped_column(VenueStatus, nullable=False, server_default="active")
    claimed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    claimed_at: Mapped[dt.datetime | None] = mapped_column(TZ)

    vibe_embedding: Mapped[Any | None] = mapped_column(Vector(1024))

    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        TZ, nullable=False, server_default=func.now()
    )

    area: Mapped[Area | None] = relationship()
    sources: Mapped[list[VenueSource]] = relationship(
        back_populates="venue", cascade="all, delete-orphan"
    )


class VenueSource(Base):
    """Provenance. Every scraped field must be traceable back to the response it came from,
    and an owner edit must never be silently overwritten by the next scrape (§10.1 rule 6)."""

    __tablename__ = "venue_source"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    venue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venue.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    scraped_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False)
    confidence: Mapped[float] = mapped_column(Real, nullable=False, server_default=text("1.0"))
    payload: Mapped[dict | None] = mapped_column(JSONB)

    venue: Mapped[Venue] = relationship(back_populates="sources")
