"""Extensions, enums and the RLS claim helpers. Plan §3.1, §4.

One addition to the plan's extension list: `citext`. §3.8 declares `app_user.email CITEXT`
but §3.1 does not enable the extension, so a literal reading of the plan fails on the auth
migration. Case-insensitive email is the right behaviour and this is where it has to be turned
on.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXTENSIONS = ["postgis", "vector", "pg_trgm", "unaccent", "btree_gist", "citext"]

ENUMS = {
    "user_role": ["diner", "owner", "admin"],
    "venue_tier": ["seeded", "claimed", "live"],
    "venue_status": ["active", "temporarily_closed", "permanently_closed", "hidden"],
    "obs_source": ["staff", "checkin", "payment", "prior", "pos"],
    "reg_event_type": ["sealed", "fined", "notice", "cleared", "reopened"],
    "offer_status": ["draft", "live", "paused", "expired"],
    "price_unit": ["per_plate", "per_kg", "half", "full", "per_person", "per_piece"],
}


def upgrade() -> None:
    for ext in EXTENSIONS:
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")

    for name, values in ENUMS.items():
        labels = ", ".join(f"'{v}'" for v in values)
        op.execute(
            f"""
            DO $$ BEGIN
              CREATE TYPE {name} AS ENUM ({labels});
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$;
            """
        )

    # The claim readers every RLS policy is written against. STABLE, not IMMUTABLE:
    # current_setting changes between transactions on the same connection, and marking these
    # IMMUTABLE would let the planner cache one caller's identity and apply it to the next.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_user_id() RETURNS UUID LANGUAGE sql STABLE AS
        $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_role() RETURNS TEXT LANGUAGE sql STABLE AS
        $$ SELECT COALESCE(NULLIF(current_setting('app.role', true), ''), 'anon') $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_group_id() RETURNS UUID LANGUAGE sql STABLE AS
        $$ SELECT NULLIF(current_setting('app.group_id', true), '')::uuid $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_slot() RETURNS SMALLINT LANGUAGE sql STABLE AS
        $$ SELECT NULLIF(current_setting('app.slot', true), '')::smallint $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_venue_id() RETURNS UUID LANGUAGE sql STABLE AS
        $$ SELECT NULLIF(current_setting('app.venue_id', true), '')::uuid $$;
        """
    )


def downgrade() -> None:
    for fn in ("app_venue_id", "app_slot", "app_group_id", "app_role", "app_user_id"):
        op.execute(f"DROP FUNCTION IF EXISTS {fn}()")
    for name in reversed(list(ENUMS)):
        op.execute(f"DROP TYPE IF EXISTS {name}")
    # Extensions are deliberately left in place. Dropping postgis cascades into every
    # geography column in the database and is never what a downgrade is meant to do.
