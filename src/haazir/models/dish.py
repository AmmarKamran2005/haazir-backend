"""Dishes, menus, price history and the Dish-Time Graph. §3.5.

`dish.name_normalized` is the join key for cross-venue price comparison. If "Bihari Boti",
"Behari boti" and "bihari-boti" do not collapse to one string then the comparison returns
nothing, no error is raised anywhere, and the feature is silently dead. The uniqueness
constraint here is the only thing making that failure loud.
"""

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
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from ._types import TZ, PriceUnit, uuid_pk


class Dish(Base):
    __tablename__ = "dish"
    __table_args__ = (
        CheckConstraint("heat_level BETWEEN 0 AND 5", name="dish_heat_level_check"),
        Index(
            "dish_norm_trgm",
            "name_normalized",
            postgresql_using="gin",
            postgresql_ops={"name_normalized": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    # Identity. Unique, and keeps the protein, so two dishes at one venue cannot collide on
    # venue_dish's (venue_id, dish_id) primary key.
    name_normalized: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Comparison. "beef bihari boti" and "chicken bihari boti" share the family
    # "bihari boti"; the endpoint groups on this and narrows by protein. See migration 0013.
    family: Mapped[str] = mapped_column(Text, nullable=False)
    protein: Mapped[str | None] = mapped_column(Text)
    name_urdu: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    heat_level: Mapped[int | None] = mapped_column(SmallInteger)
    ingredients: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    allergens: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    dietary_flags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    embedding: Mapped[Any | None] = mapped_column(Vector(1024))


class VenueDish(Base):
    __tablename__ = "venue_dish"

    venue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venue.id", ondelete="CASCADE"), primary_key=True
    )
    dish_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dish.id"), primary_key=True
    )
    menu_name: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    price_pkr: Mapped[int | None] = mapped_column(Integer)
    price_unit: Mapped[str] = mapped_column(PriceUnit, nullable=False, server_default="per_plate")
    # Under 20%+ food inflation a price with no date is worse than no price.
    price_seen_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    price_source: Mapped[str | None] = mapped_column(Text)
    quality_mean: Mapped[float | None] = mapped_column(Real)
    quality_n: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_signature: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    sold_out_until: Mapped[dt.datetime | None] = mapped_column(TZ)

    dish: Mapped[Dish] = relationship()


class VenueDishPrice(Base):
    """Append-only price history. Both the comparison endpoint and the trend read this."""

    __tablename__ = "venue_dish_price"
    __table_args__ = (
        ForeignKeyConstraint(
            ["venue_id", "dish_id"],
            ["venue_dish.venue_id", "venue_dish.dish_id"],
            ondelete="CASCADE",
        ),
        Index("vdp_lookup", "dish_id", text("seen_at DESC")),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    dish_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    price_pkr: Mapped[int] = mapped_column(Integer, nullable=False)
    seen_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)


class DishTimeQuality(Base):
    """The Dish-Time Graph. Quality as f(dish, venue, hour_of_week).

    This is the table that makes the product's second load-bearing claim true: Javed Nihari's
    nihari is a 9.4 before 10am and a 5.4 at 8pm, and a venue-level star rating destroys that.
    """

    __tablename__ = "dish_time_quality"
    __table_args__ = (
        CheckConstraint("hour_of_week BETWEEN 0 AND 167", name="dtq_how_check"),
        ForeignKeyConstraint(
            ["venue_id", "dish_id"],
            ["venue_dish.venue_id", "venue_dish.dish_id"],
            ondelete="CASCADE",
        ),
    )

    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    dish_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    hour_of_week: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    quality: Mapped[float] = mapped_column(Real, nullable=False)
    confidence: Mapped[float] = mapped_column(Real, nullable=False)
    n_observations: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class DishAvailability(Base):
    """When a dish typically runs out. "Nihari khatam ho jati hai 11 baje" as a number."""

    __tablename__ = "dish_availability"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="dish_avail_weekday_check"),
        ForeignKeyConstraint(
            ["venue_id", "dish_id"],
            ["venue_dish.venue_id", "venue_dish.dish_id"],
            ondelete="CASCADE",
        ),
    )

    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    dish_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    weekday: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    typical_sellout_minute: Mapped[int | None] = mapped_column(SmallInteger)
    sellout_sigma: Mapped[float | None] = mapped_column(Real)
