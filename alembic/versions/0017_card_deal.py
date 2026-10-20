"""Card deals: "20% off with your HBL card". Plan §4 B7.

A deal belongs either to one venue (`venue_id`) or to a chain (`chain_name`, matched against
the start of a venue's name at read time, so a branch added later is covered without a
reload). `days_of_week` uses ISO numbering, 1 = Monday to 7 = Sunday, as the seed file does.

`is_demo` marks rows a bank has not actually agreed to. Only an admin sees the flag; every
other reader sees the deal as the interface shows it.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE card_deal (
          id                SERIAL PRIMARY KEY,
          ext_id            TEXT UNIQUE,
          bank              TEXT NOT NULL,
          card_tier         TEXT,
          venue_id          UUID REFERENCES venue(id) ON DELETE CASCADE,
          chain_name        TEXT,
          discount_pct      INT NOT NULL,
          max_discount_pkr  INT,
          days_of_week      INT[] NOT NULL DEFAULT '{1,2,3,4,5,6,7}',
          valid_until       DATE,
          terms             TEXT,
          is_demo           BOOLEAN NOT NULL DEFAULT FALSE,
          created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT card_deal_target CHECK (venue_id IS NOT NULL OR chain_name IS NOT NULL),
          CONSTRAINT card_deal_pct CHECK (discount_pct BETWEEN 1 AND 90)
        );
        """
    )
    op.execute("CREATE INDEX card_deal_venue ON card_deal (venue_id) WHERE venue_id IS NOT NULL")
    op.execute("CREATE INDEX card_deal_bank ON card_deal (lower(bank))")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS card_deal")
