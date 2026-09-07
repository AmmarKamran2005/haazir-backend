"""Live state: the sensor stream, the fused estimate, priors and per-source calibration. §3.6.

`observation` is the append-only sensor log and the highest-write table in the product. It is
declared here for the ORM but the partitioning, which SQLAlchemy cannot express, lives in the
migration. It deliberately carries no foreign key to `venue`: a referential check on every
insert buys nothing on a stream whose venue ids all come from a join we already did, and it
costs on the one path that has to stay cheap.

Nothing here stores an identity. An observation is a count and a timestamp.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    REAL as Real,
)
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from ._types import TZ, ObsSource


class Observation(Base):
    __tablename__ = "observation"
    __table_args__ = (
        CheckConstraint("value BETWEEN 0 AND 1", name="observation_value_check"),
        Index("observation_venue_time", "venue_id", text("observed_at DESC")),
        {"postgresql_partition_by": "RANGE (observed_at)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    venue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(ObsSource, nullable=False)
    observed_at: Mapped[dt.datetime] = mapped_column(TZ, primary_key=True)
    value: Mapped[float] = mapped_column(Real, nullable=False)
    # Per observation, not per source. See §6.2: the relative error on N payment ticks is
    # 1/sqrt(N), so a quiet minute must not be fused as if it were a busy one.
    sigma: Mapped[float] = mapped_column(Real, nullable=False)
    reporter_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reporter_trust: Mapped[float] = mapped_column(Real, nullable=False, server_default=text("0.5"))
    geo_ok: Mapped[bool | None] = mapped_column(Boolean)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class LiveState(Base):
    """The current fused estimate. One row per venue, overwritten in place.

    `source_weights` is not diagnostics. It is shown to the user, because an occupancy number
    without its provenance is the same unfalsifiable claim every other app already makes.
    """

    __tablename__ = "live_state"

    venue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venue.id", ondelete="CASCADE"), primary_key=True
    )
    occupancy: Mapped[float] = mapped_column(Real, nullable=False)
    sd: Mapped[float] = mapped_column(Real, nullable=False)
    wait_p50_min: Mapped[float] = mapped_column(Real, nullable=False)
    wait_p90_min: Mapped[float] = mapped_column(Real, nullable=False)
    trend: Mapped[float] = mapped_column(Real, nullable=False, server_default=text("0"))
    confidence: Mapped[float] = mapped_column(Real, nullable=False)
    band: Mapped[str] = mapped_column(Text, nullable=False)
    source_weights: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        TZ, nullable=False, server_default=text("now()")
    )


class OccupancyPrior(Base):
    """168 rows per venue: the baseline the live estimate corrects against.

    Built from Google `popular_times` where it exists, from a category archetype where it does
    not. `source` says which, and the API passes that through, because a prior dressed up as a
    measurement is the exact failure this product exists to avoid.
    """

    __tablename__ = "occupancy_prior"
    __table_args__ = (CheckConstraint("hour_of_week BETWEEN 0 AND 167", name="prior_how_check"),)

    venue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venue.id", ondelete="CASCADE"), primary_key=True
    )
    hour_of_week: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    mean_ratio: Mapped[float] = mapped_column(Real, nullable=False)
    sigma: Mapped[float] = mapped_column(Real, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)


class SourceCalibration(Base):
    """Learned per-venue, per-source bias and variance.

    A venue whose staff always taps "busy" gets a bias correction rather than being trusted or
    discarded wholesale. This is what turns the fusion weights from constants into something
    that improves with use.
    """

    __tablename__ = "source_calibration"

    venue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("venue.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(ObsSource, primary_key=True)
    bias: Mapped[float] = mapped_column(Real, nullable=False, server_default=text("0"))
    variance: Mapped[float] = mapped_column(Real, nullable=False, server_default=text("0.04"))
    trust: Mapped[float] = mapped_column(Real, nullable=False, server_default=text("0.5"))
    n: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
