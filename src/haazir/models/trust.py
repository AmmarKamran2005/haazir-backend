"""Trust: regulatory record, right of reply, computed score, review features. §3.7.

Two rules are structural here rather than procedural.

`regulatory_event.source_url` is NOT NULL. A hygiene claim about a named restaurant without a
citation is an accusation, so the schema refuses to hold one.

`review_sample` has no text column. Reviews are read once, turned into features and a vector,
and the prose is dropped. Keeping a corpus of other people's writing indefinitely is a
liability with no product upside.
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
    Date,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ._types import TZ, RegEventType, created_at, uuid_pk


class RegulatoryEvent(Base):
    __tablename__ = "regulatory_event"
    __table_args__ = (Index("reg_event_venue", "venue_id", text("event_date DESC")),)

    id: Mapped[uuid.UUID] = uuid_pk()
    venue_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venue.id", ondelete="SET NULL")
    )
    authority: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(RegEventType, nullable=False)
    event_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    fine_pkr: Mapped[int | None] = mapped_column(Integer)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    raw_venue_name: Mapped[str] = mapped_column(Text, nullable=False)
    # Below 0.90 this stays unpublished and goes to a human. A wrong match publishes
    # "sealed for expired meat" against an innocent business.
    match_confidence: Mapped[float] = mapped_column(Real, nullable=False)
    published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    created_at: Mapped[dt.datetime] = created_at()


class RegulatoryReply(Base):
    """A venue may always answer a record shown about it. There is no endpoint that deletes
    a `regulatory_event`; this is the only response path, by design."""

    __tablename__ = "regulatory_reply"

    id: Mapped[uuid.UUID] = uuid_pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("regulatory_event.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[dt.datetime] = created_at()


class TrustScore(Base):
    """`components` is not a breakdown of the score. It is the score; the integer is a
    read-out of it. The API returns both so a venue can see exactly what to fix."""

    __tablename__ = "trust_score"
    __table_args__ = (CheckConstraint("score BETWEEN 0 AND 100", name="trust_score_range_check"),)

    venue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venue.id", ondelete="CASCADE"), primary_key=True
    )
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    components: Mapped[dict] = mapped_column(JSONB, nullable=False)
    computed_at: Mapped[dt.datetime] = mapped_column(
        TZ, nullable=False, server_default=text("now()")
    )


class ReviewSample(Base):
    __tablename__ = "review_sample"
    __table_args__ = (
        UniqueConstraint("venue_id", "external_id", name="review_sample_venue_external_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    venue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venue.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    lang: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    # sha256 of the reviewer's name. Enough to detect one author flooding a venue,
    # not enough to identify them.
    author_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_local_guide: Mapped[bool | None] = mapped_column(Boolean)
    photo_count: Mapped[int | None] = mapped_column(SmallInteger)
    embedding: Mapped[Any | None] = mapped_column(Vector(1024))
    flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    flag_reason: Mapped[str | None] = mapped_column(Text)
