"""Offers and attribution. Plan §3.10.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE offer (
          id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          venue_id      UUID NOT NULL REFERENCES venue(id) ON DELETE CASCADE,
          weekday       SMALLINT,
          window_start  TIME NOT NULL,
          window_end    TIME NOT NULL,
          discount_pct  SMALLINT NOT NULL,
          cap_covers    SMALLINT,
          status        offer_status NOT NULL DEFAULT 'draft',
          created_by    UUID REFERENCES app_user(id),
          created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          expires_at    TIMESTAMPTZ,
          CONSTRAINT offer_discount_check CHECK (discount_pct BETWEEN 1 AND 60),
          CONSTRAINT offer_weekday_check CHECK (weekday IS NULL OR weekday BETWEEN 0 AND 6)
        );
        """
    )
    op.execute(
        "CREATE INDEX offer_live ON offer (venue_id, weekday) WHERE status = 'live'"
    )

    op.execute(
        """
        CREATE TABLE attribution (
          id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          venue_id         UUID NOT NULL REFERENCES venue(id) ON DELETE CASCADE,
          user_id          UUID REFERENCES app_user(id) ON DELETE SET NULL,
          visit_id         UUID REFERENCES visit(id) ON DELETE SET NULL,
          referred_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
          seated_at        TIMESTAMPTZ,
          receipt_verified BOOLEAN NOT NULL DEFAULT FALSE,
          amount_pkr       INT
        );
        """
    )
    op.execute("CREATE INDEX attribution_venue_time ON attribution (venue_id, referred_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS attribution")
    op.execute("DROP TABLE IF EXISTS offer")
