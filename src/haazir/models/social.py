"""Visits, crowd-verified facts, and groups. §3.9.

`group_constraint` is the privacy table. Nothing in the product may return a row of it to
anyone but its own author: not to the other members, not to the group creator, not to an
admin. That is enforced in the database by FORCE ROW LEVEL SECURITY with no policy granting
creator access, and it has a test, because a promise like this is worth exactly as much as
the test that proves it.

The output of a solve is `group_solution.satisfaction`, which carries a slot, a name and a
utility number, and never a budget or a restriction.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    REAL as Real,
)
from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ._types import TZ, created_at, uuid_pk


class Visit(Base):
    __tablename__ = "visit"
    __table_args__ = (Index("visit_venue_time", "venue_id", text("arrived_at DESC")),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL")
    )
    venue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venue.id", ondelete="CASCADE"), nullable=False
    )
    party_size: Mapped[int | None] = mapped_column(SmallInteger)
    arrived_at: Mapped[dt.datetime] = mapped_column(
        TZ, nullable=False, server_default=text("now()")
    )
    seated_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    wait_reported_min: Mapped[int | None] = mapped_column(SmallInteger)
    receipt_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    spend_pkr: Mapped[int | None] = mapped_column(Integer)
    referred_by_haazir: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class FactVerification(Base):
    """One diner confirming or contradicting an access fact. Aggregated nightly into
    `venue.attributes`, where the running count and the decayed confidence live."""

    __tablename__ = "fact_verification"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    venue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venue.id", ondelete="CASCADE"), nullable=False
    )
    fact_key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL")
    )
    weight: Mapped[float] = mapped_column(Real, nullable=False, server_default=text("1.0"))
    created_at: Mapped[dt.datetime] = created_at()


class GroupSession(Base):
    __tablename__ = "group_session"

    id: Mapped[uuid.UUID] = uuid_pk()
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default="Dinner")
    city_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("city.id"), nullable=False)
    from_area_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("area.id"))
    party_size: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="collecting")
    created_at: Mapped[dt.datetime] = created_at()
    expires_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False)


class GroupMember(Base):
    __tablename__ = "group_member"
    __table_args__ = (UniqueConstraint("group_id", "slot", name="group_member_group_slot_key"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("group_session.id", ondelete="CASCADE"), nullable=False
    )
    slot: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL")
    )
    responded_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    # Raised above 1.0 for someone who compromised last time. The person who always gives way
    # is the reason group decisions quietly stop happening.
    weight: Mapped[float] = mapped_column(Real, nullable=False, server_default=text("1.0"))
    regret_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))


class GroupConstraint(Base):
    """THE PRIVACY TABLE. See the module docstring before adding any query against it."""

    __tablename__ = "group_constraint"

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("group_session.id", ondelete="CASCADE"), primary_key=True
    )
    member_slot: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    budget_pkr: Mapped[int | None] = mapped_column(Integer)
    max_travel_min: Mapped[int | None] = mapped_column(SmallInteger)
    diet: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    mood: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[dt.datetime] = mapped_column(
        TZ, nullable=False, server_default=text("now()")
    )


class GroupSolution(Base):
    __tablename__ = "group_solution"

    id: Mapped[uuid.UUID] = uuid_pk()
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("group_session.id", ondelete="CASCADE"), nullable=False
    )
    venue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venue.id"), nullable=False
    )
    objective: Mapped[float] = mapped_column(Real, nullable=False)
    # The objective maximises the minimum, not the mean. Both are stored so the difference
    # between "everyone is fine" and "four are delighted and one is miserable" stays visible.
    min_sat: Mapped[float] = mapped_column(Real, nullable=False)
    mean_sat: Mapped[float] = mapped_column(Real, nullable=False)
    satisfaction: Mapped[list] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    solved_at: Mapped[dt.datetime] = mapped_column(
        TZ, nullable=False, server_default=text("now()")
    )
