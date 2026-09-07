"""Offers and attribution. §3.10.

An offer changes the price shown on a venue a diner already matched. It never changes the
order of results and it is always labelled (§14 rule 7). The schema keeps offers in their own
table for exactly that reason: there is no column on `venue` that a payment can move.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, SmallInteger, Time, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ._types import TZ, OfferStatus, created_at, uuid_pk


class Offer(Base):
    __tablename__ = "offer"
    __table_args__ = (
        CheckConstraint("discount_pct BETWEEN 1 AND 60", name="offer_discount_check"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    venue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venue.id", ondelete="CASCADE"), nullable=False
    )
    weekday: Mapped[int | None] = mapped_column(SmallInteger)
    window_start: Mapped[dt.time] = mapped_column(Time, nullable=False)
    window_end: Mapped[dt.time] = mapped_column(Time, nullable=False)
    discount_pct: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    cap_covers: Mapped[int | None] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(OfferStatus, nullable=False, server_default="draft")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id")
    )
    created_at: Mapped[dt.datetime] = created_at()
    expires_at: Mapped[dt.datetime | None] = mapped_column(TZ)


class Attribution(Base):
    """Proof that HAAZIR sent a guest who actually sat down and spent.

    This is the table the business model rests on, which is why `receipt_verified` is separate
    from `seated_at`: a referral that cannot be verified is reported as unverified rather than
    counted.
    """

    __tablename__ = "attribution"

    id: Mapped[uuid.UUID] = uuid_pk()
    venue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venue.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL")
    )
    visit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("visit.id", ondelete="SET NULL")
    )
    referred_at: Mapped[dt.datetime] = mapped_column(
        TZ, nullable=False, server_default=text("now()")
    )
    seated_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    receipt_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    amount_pkr: Mapped[int | None] = mapped_column(Integer)
