"""Owner-edited menus and offers. A portion label on a menu line; a title and days on offers.

`venue_dish.portion` is the label as the restaurant writes it ("1 pound", "half kg", "serves
4"). `price_unit` stays the coarse enum the comparison groups on; the label is what a diner
reads. `offer` gains a title and a set of weekdays (0 = Sunday, as `weekday` already was), so
one row can say "Mon to Thu, 3 to 6 pm" instead of four.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE venue_dish ADD COLUMN portion TEXT")
    op.execute("ALTER TABLE venue_dish ADD COLUMN owner_added BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE offer ADD COLUMN title TEXT")
    op.execute("ALTER TABLE offer ADD COLUMN days SMALLINT[]")


def downgrade() -> None:
    op.execute("ALTER TABLE offer DROP COLUMN IF EXISTS days")
    op.execute("ALTER TABLE offer DROP COLUMN IF EXISTS title")
    op.execute("ALTER TABLE venue_dish DROP COLUMN IF EXISTS owner_added")
    op.execute("ALTER TABLE venue_dish DROP COLUMN IF EXISTS portion")
