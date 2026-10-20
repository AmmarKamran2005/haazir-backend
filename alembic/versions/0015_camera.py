"""Cameras. A fourth live source: on-device table detection.

A camera box (a laptop webcam, a phone running an MJPEG app, an RTSP IP camera) runs the
detector locally and sends only counts. The video never leaves the venue; what arrives here
is "4 of 6 tables occupied, 11 people" every few seconds, and that becomes an `observation`
with `source = 'camera'` like any other sensor reading.

**Zones live on the camera row as JSONB.** They are drawn and edited as one document by the
vision box and read back as one document; nothing queries into an individual zone, so a
child table would be ceremony.

**The token is a bearer credential with its own prefix.** `hzc_`, stored as a SHA-256 hash,
exactly like a staff device. A camera is not a person and not a staff tablet: it can write
counts for its own venue and nothing else.

**The latest frame is kept in Postgres.** One small, already face-blurred JPEG per camera,
overwritten in place. A Fly volume or object storage would be the right home at scale; at
the scale of a handful of venues, one `bytea` column is less to operate.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A new enum label cannot be used inside the transaction that adds it, and nothing below
    # uses it; it still goes in its own block so a later step in this file can.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE obs_source ADD VALUE IF NOT EXISTS 'camera'")

    op.execute(
        """
        CREATE TABLE camera (
          id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          venue_id         UUID NOT NULL REFERENCES venue(id) ON DELETE CASCADE,
          name             TEXT NOT NULL,
          kind             TEXT NOT NULL,
          token_hash       TEXT NOT NULL UNIQUE,
          frame_w          INT,
          frame_h          INT,
          zones            JSONB NOT NULL DEFAULT '[]'::jsonb,
          last_seen        TIMESTAMPTZ,
          people           INT,
          tables_total     INT,
          tables_occupied  INT,
          fps              REAL,
          latest_tables    JSONB,
          snapshot         BYTEA,
          snapshot_at      TIMESTAMPTZ,
          created_by       UUID REFERENCES app_user(id) ON DELETE SET NULL,
          created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
          revoked_at       TIMESTAMPTZ,
          CONSTRAINT camera_kind_check CHECK (kind IN ('webcam', 'phone', 'rtsp'))
        );
        """
    )
    op.execute(
        "CREATE INDEX camera_venue ON camera (venue_id, last_seen DESC) "
        "WHERE revoked_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS camera")
    # Postgres cannot drop an enum label. Observations written with it stay valid.
