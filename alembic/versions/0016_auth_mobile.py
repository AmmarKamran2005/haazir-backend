"""Mobile sign-in: a 6-digit code beside the magic link, and Google identity on the user.

A phone cannot follow a magic link back into an app without deep-link plumbing, so the same
email carries a short code. It shares the `magic_link` row: one email, one expiry, one
consumption, two ways to redeem it. The code is stored hashed with the address as salt, and
`code_attempts` caps guessing at a handful of tries per issued code.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE magic_link ADD COLUMN code_hash TEXT")
    op.execute("ALTER TABLE magic_link ADD COLUMN code_attempts INT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE app_user ADD COLUMN avatar_url TEXT")
    op.execute("ALTER TABLE app_user ADD COLUMN google_sub TEXT UNIQUE")


def downgrade() -> None:
    op.execute("ALTER TABLE app_user DROP COLUMN IF EXISTS google_sub")
    op.execute("ALTER TABLE app_user DROP COLUMN IF EXISTS avatar_url")
    op.execute("ALTER TABLE magic_link DROP COLUMN IF EXISTS code_attempts")
    op.execute("ALTER TABLE magic_link DROP COLUMN IF EXISTS code_hash")
