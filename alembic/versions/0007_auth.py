"""Auth. Plan §3.8.

Every token table stores `token_hash` and nothing else. A partial unique index on
`magic_link` makes single-use structural: once `consumed_at` is set the row can no longer
satisfy a live lookup, and the application never needs to be trusted to delete it in time.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app_user (
          id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          email           CITEXT UNIQUE NOT NULL,
          email_verified  BOOLEAN NOT NULL DEFAULT FALSE,
          display_name    TEXT,
          role            user_role NOT NULL DEFAULT 'diner',
          home_area_id    INT REFERENCES area(id),
          palate          JSONB NOT NULL DEFAULT '{}'::jsonb,
          reputation      REAL NOT NULL DEFAULT 0.5,
          status          TEXT NOT NULL DEFAULT 'active',
          created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_seen_at    TIMESTAMPTZ,
          CONSTRAINT app_user_reputation_check CHECK (reputation BETWEEN 0 AND 1),
          CONSTRAINT app_user_status_check CHECK (status IN ('active','suspended','deleted'))
        );
        """
    )

    # Deferred from 0003: venue existed before app_user did.
    op.execute(
        """
        ALTER TABLE venue ADD CONSTRAINT venue_claimed_by_fk
        FOREIGN KEY (claimed_by) REFERENCES app_user(id) ON DELETE SET NULL;
        """
    )
    op.execute(
        "CREATE INDEX venue_claimed_by ON venue (claimed_by) WHERE claimed_by IS NOT NULL"
    )

    op.execute(
        """
        CREATE TABLE magic_link (
          id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          email       CITEXT NOT NULL,
          token_hash  TEXT NOT NULL UNIQUE,
          purpose     TEXT NOT NULL DEFAULT 'login',
          expires_at  TIMESTAMPTZ NOT NULL,
          consumed_at TIMESTAMPTZ,
          request_ip  INET,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX magic_link_email_time ON magic_link (email, created_at DESC)")
    # Rate limiting (§5 rule 4) counts recent links per email and per IP. Both are covered.
    op.execute(
        "CREATE INDEX magic_link_ip_time ON magic_link (request_ip, created_at DESC) "
        "WHERE request_ip IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX magic_link_live ON magic_link (token_hash) "
        "WHERE consumed_at IS NULL"
    )

    op.execute(
        """
        CREATE TABLE refresh_token (
          id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id     UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
          family_id   UUID NOT NULL,
          token_hash  TEXT NOT NULL UNIQUE,
          expires_at  TIMESTAMPTZ NOT NULL,
          revoked_at  TIMESTAMPTZ,
          replaced_by UUID REFERENCES refresh_token(id),
          user_agent  TEXT,
          ip          INET,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    # Reuse detection revokes by family, so the family has to be the cheap lookup.
    op.execute("CREATE INDEX refresh_family ON refresh_token (family_id)")
    op.execute("CREATE INDEX refresh_user ON refresh_token (user_id, created_at DESC)")

    op.execute(
        """
        CREATE TABLE device_token (
          id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          venue_id     UUID NOT NULL REFERENCES venue(id) ON DELETE CASCADE,
          token_hash   TEXT NOT NULL UNIQUE,
          label        TEXT,
          geofence_m   INT NOT NULL DEFAULT 300,
          expires_at   TIMESTAMPTZ NOT NULL,
          last_used_at TIMESTAMPTZ,
          revoked_at   TIMESTAMPTZ,
          created_by   UUID REFERENCES app_user(id),
          created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT device_geofence_check CHECK (geofence_m BETWEEN 50 AND 2000)
        );
        """
    )
    op.execute("CREATE INDEX device_token_venue ON device_token (venue_id) WHERE revoked_at IS NULL")

    op.execute(
        """
        CREATE TABLE group_token (
          id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          group_id    UUID NOT NULL,
          member_slot SMALLINT NOT NULL,
          token_hash  TEXT NOT NULL UNIQUE,
          expires_at  TIMESTAMPTZ NOT NULL,
          consumed_at TIMESTAMPTZ,
          CONSTRAINT group_token_group_slot_key UNIQUE (group_id, member_slot)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS group_token")
    op.execute("DROP TABLE IF EXISTS device_token")
    op.execute("DROP TABLE IF EXISTS refresh_token")
    op.execute("DROP TABLE IF EXISTS magic_link")
    op.execute("DROP INDEX IF EXISTS venue_claimed_by")
    op.execute("ALTER TABLE venue DROP CONSTRAINT IF EXISTS venue_claimed_by_fk")
    op.execute("DROP TABLE IF EXISTS app_user")
