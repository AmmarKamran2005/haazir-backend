"""Restaurant claims and registrations, decided by an admin. Plan §4 B2.

A claim is either for a venue we already list (`venue_id`) or for one we do not
(`new_venue`, the owner's own details as JSON until an admin approves and it becomes a row).
Approval sets `venue.claimed_by` and promotes the user to `owner`; nothing about the venue
changes before that.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE venue_claim (
          id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id     UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
          venue_id    UUID REFERENCES venue(id) ON DELETE CASCADE,
          new_venue   JSONB,
          proof_note  TEXT,
          status      TEXT NOT NULL DEFAULT 'pending',
          decided_by  UUID REFERENCES app_user(id) ON DELETE SET NULL,
          decided_at  TIMESTAMPTZ,
          note        TEXT,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT venue_claim_status CHECK (status IN ('pending', 'approved', 'rejected')),
          CONSTRAINT venue_claim_target CHECK ((venue_id IS NULL) <> (new_venue IS NULL))
        );
        """
    )
    op.execute("CREATE INDEX venue_claim_status_time ON venue_claim (status, created_at DESC)")
    op.execute("CREATE INDEX venue_claim_user ON venue_claim (user_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS venue_claim")
