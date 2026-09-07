"""Users, magic links, refresh families, staff devices, group guests. §3.8.

Four token tables, one rule shared by all of them: the column is `token_hash`, never `token`.
A database dump must not be a set of working credentials.

`device_token` deserves its comment. Staff auth is a device, not a person. Restaurant staff
turnover in Karachi is high and a login tied to a waiter dies with their employment; a token
tied to the counter tablet survives it. That decision is also why no managed auth provider
could have served this product.
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
from sqlalchemy.dialects.postgresql import CITEXT, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ._types import TZ, UserRole, created_at, uuid_pk


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    display_name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(UserRole, nullable=False, server_default="diner")
    home_area_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("area.id"))
    # spice, richness, novelty, sweet, seafood. Learned from visits, never asked in a quiz.
    palate: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Weights this user's check-ins in the fusion step. A new account counts for half.
    reputation: Mapped[float] = mapped_column(Real, nullable=False, server_default=text("0.5"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[dt.datetime] = created_at()
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(TZ)


class MagicLink(Base):
    __tablename__ = "magic_link"
    __table_args__ = (Index("magic_link_email_time", "email", text("created_at DESC")),)

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    purpose: Mapped[str] = mapped_column(Text, nullable=False, server_default="login")
    expires_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    request_ip: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[dt.datetime] = created_at()


class RefreshToken(Base):
    """Rotation with reuse detection.

    Every issued refresh token belongs to a `family_id`. Presenting a token that has already
    been rotated means it was captured, so the whole family is revoked rather than just that
    token. The legitimate holder is logged out, which is the correct outcome: an attacker
    holding a valid refresh token is worse than an inconvenienced user.
    """

    __tablename__ = "refresh_token"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("refresh_token.id")
    )
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[dt.datetime] = created_at()


class DeviceToken(Base):
    __tablename__ = "device_token"

    id: Mapped[uuid.UUID] = uuid_pk()
    venue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venue.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    label: Mapped[str | None] = mapped_column(Text)
    # A state update from outside this radius is rejected with 409 and still recorded, so a
    # gaming attempt shows up in the data rather than vanishing.
    geofence_m: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("300"))
    expires_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app_user.id")
    )
    created_at: Mapped[dt.datetime] = created_at()


class GroupToken(Base):
    """One scoped token per group member slot. It authorises writing that slot's constraint
    and nothing else, including reading it back later."""

    __tablename__ = "group_token"
    __table_args__ = (
        UniqueConstraint("group_id", "member_slot", name="group_token_group_slot_key"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    member_slot: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[dt.datetime] = mapped_column(TZ, nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(TZ)
