"""Give a dish a family and a protein. Plan §3.5, extending it.

**Why this is not in the plan's DDL, and why it has to exist.** §3.5 makes
`dish.name_normalized` unique and calls it "the join key for cross-venue price comparison",
and `docs/SCRAPING-PROMPT.md` §5 illustrates the intent with `nihari <- ... beef nihari,
maghaz nihari`. Read literally, protein qualifiers collapse into the base dish, and that
breaks two things at once.

`venue_dish` is keyed `(venue_id, dish_id)`. A venue selling both Beef Bihari Boti and
Chicken Bihari Boti would collide on insert and lose one of them. Worse, the comparison the
join exists to serve would put a beef price and a chicken price in the same bucket and report
a median across them: a confident wrong answer, which is the specific failure this product is
built to avoid.

So identity and comparison are separated. `name_normalized` stays the full dish
("beef bihari boti") and remains unique. `family` is the comparison key ("bihari boti") and
`protein` is the facet, so `/dishes/{family}/prices` can group honestly and narrow to one
protein when a caller wants like for like.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE dish ADD COLUMN family TEXT")
    op.execute("ALTER TABLE dish ADD COLUMN protein TEXT")

    # Backfill for any rows that predate this migration: a dish with no family is its own
    # family, which is the correct answer for a single-word dish and a harmless one otherwise.
    op.execute("UPDATE dish SET family = name_normalized WHERE family IS NULL")
    op.execute("ALTER TABLE dish ALTER COLUMN family SET NOT NULL")

    op.execute(
        "ALTER TABLE dish ADD CONSTRAINT dish_protein_check CHECK ("
        "protein IS NULL OR protein IN "
        "('beef','mutton','chicken','fish','prawn','vegetarian'))"
    )

    # The price-comparison endpoint filters on family and nothing else, across every menu line
    # in the city. Without this it is a sequential scan of `dish` on every call.
    op.execute("CREATE INDEX dish_family ON dish (family)")
    op.execute(
        "CREATE INDEX dish_family_trgm ON dish USING GIN (family gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS dish_family_trgm")
    op.execute("DROP INDEX IF EXISTS dish_family")
    op.execute("ALTER TABLE dish DROP CONSTRAINT IF EXISTS dish_protein_check")
    op.execute("ALTER TABLE dish DROP COLUMN IF EXISTS protein")
    op.execute("ALTER TABLE dish DROP COLUMN IF EXISTS family")
