"""The base a learned prior is blended from. Plan §4 B9.

The nightly job moves a venue's hour-of-week prior toward what its cameras and staff actually
reported in that hour. Blending into the current `mean_ratio` every night would compound, the
same way the fact decay once did (0014): a night's update would be applied on top of every
earlier night's. Keeping the original figure as `base_ratio` makes the job a function of the
evidence alone, so running it twice gives the same answer as running it once.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE occupancy_prior ADD COLUMN base_ratio REAL")
    op.execute("ALTER TABLE occupancy_prior ADD COLUMN base_sigma REAL")
    op.execute("ALTER TABLE occupancy_prior ADD COLUMN base_source TEXT")
    op.execute("ALTER TABLE occupancy_prior ADD COLUMN learned_days INT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE occupancy_prior ADD COLUMN learned_at TIMESTAMPTZ")


def downgrade() -> None:
    for col in ("learned_at", "learned_days", "base_source", "base_sigma", "base_ratio"):
        op.execute(f"ALTER TABLE occupancy_prior DROP COLUMN IF EXISTS {col}")
