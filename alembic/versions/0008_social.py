"""Visits, fact verification, groups. Plan §3.9.

`group_token.group_id` gets its foreign key here rather than in 0007, because `group_session`
did not exist yet.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE visit (
          id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id            UUID REFERENCES app_user(id) ON DELETE SET NULL,
          venue_id           UUID NOT NULL REFERENCES venue(id) ON DELETE CASCADE,
          party_size         SMALLINT,
          arrived_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
          seated_at          TIMESTAMPTZ,
          wait_reported_min  SMALLINT,
          receipt_verified   BOOLEAN NOT NULL DEFAULT FALSE,
          spend_pkr          INT,
          referred_by_haazir BOOLEAN NOT NULL DEFAULT FALSE
        );
        """
    )
    op.execute("CREATE INDEX visit_venue_time ON visit (venue_id, arrived_at DESC)")
    op.execute(
        "CREATE INDEX visit_user_time ON visit (user_id, arrived_at DESC) "
        "WHERE user_id IS NOT NULL"
    )

    op.execute(
        """
        CREATE TABLE fact_verification (
          id         BIGSERIAL PRIMARY KEY,
          venue_id   UUID NOT NULL REFERENCES venue(id) ON DELETE CASCADE,
          fact_key   TEXT NOT NULL,
          value      JSONB NOT NULL,
          user_id    UUID REFERENCES app_user(id) ON DELETE SET NULL,
          weight     REAL NOT NULL DEFAULT 1.0,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX fact_verification_lookup ON fact_verification (venue_id, fact_key)")

    op.execute(
        """
        CREATE TABLE group_session (
          id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          creator_id   UUID REFERENCES app_user(id) ON DELETE SET NULL,
          title        TEXT NOT NULL DEFAULT 'Dinner',
          city_id      SMALLINT NOT NULL REFERENCES city(id),
          from_area_id INT REFERENCES area(id),
          party_size   SMALLINT NOT NULL,
          status       TEXT NOT NULL DEFAULT 'collecting',
          created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
          expires_at   TIMESTAMPTZ NOT NULL,
          CONSTRAINT group_status_check CHECK (status IN ('collecting','solved','closed')),
          CONSTRAINT group_party_check CHECK (party_size BETWEEN 2 AND 30)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE group_member (
          id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          group_id     UUID NOT NULL REFERENCES group_session(id) ON DELETE CASCADE,
          slot         SMALLINT NOT NULL,
          display_name TEXT NOT NULL,
          user_id      UUID REFERENCES app_user(id) ON DELETE SET NULL,
          responded_at TIMESTAMPTZ,
          weight       REAL NOT NULL DEFAULT 1.0,
          regret_count SMALLINT NOT NULL DEFAULT 0,
          CONSTRAINT group_member_group_slot_key UNIQUE (group_id, slot)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE group_constraint (
          group_id       UUID NOT NULL REFERENCES group_session(id) ON DELETE CASCADE,
          member_slot    SMALLINT NOT NULL,
          budget_pkr     INT,
          max_travel_min SMALLINT,
          diet           TEXT[] NOT NULL DEFAULT '{}',
          mood           TEXT,
          submitted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (group_id, member_slot)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE group_solution (
          id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          group_id     UUID NOT NULL REFERENCES group_session(id) ON DELETE CASCADE,
          venue_id     UUID NOT NULL REFERENCES venue(id),
          objective    REAL NOT NULL,
          min_sat      REAL NOT NULL,
          mean_sat     REAL NOT NULL,
          satisfaction JSONB NOT NULL,
          rationale    TEXT,
          solved_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX group_solution_group ON group_solution (group_id, solved_at DESC)")

    op.execute(
        """
        ALTER TABLE group_token ADD CONSTRAINT group_token_group_fk
        FOREIGN KEY (group_id) REFERENCES group_session(id) ON DELETE CASCADE;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE group_token DROP CONSTRAINT IF EXISTS group_token_group_fk")
    for t in (
        "group_solution",
        "group_constraint",
        "group_member",
        "group_session",
        "fact_verification",
        "visit",
    ):
        op.execute(f"DROP TABLE IF EXISTS {t}")
