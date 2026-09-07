"""Row-level security. Plan §4.

**Why FORCE, on every table here.** `ENABLE ROW LEVEL SECURITY` alone does not apply to the
table's owner. On Neon the API, the migrations and the workers all arrive as the same role
that created the tables, so `ENABLE` on its own would leave every policy in this file inert
while looking, in `\\d`, exactly like a database that was secured. `FORCE` closes that, and
the price is that the service paths need an explicit way through.

**The two service flags.** `app.service` and `app.solver` are settings that no request can
produce. `Claims.as_settings()` in `db.py` writes both as empty strings on every request
transaction, so nothing survives on a pooled connection, and no JWT role maps to either. They
are set only by `service_session()` and `solver_session()`, which is a short and greppable
list of call sites.

`group_constraint` gets no service policy at all. The solver reads it through `app.solver` and
returns a satisfaction vector; nothing else in the product may read another member's row.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RLS_TABLES = [
    "venue",
    "observation",
    "regulatory_event",
    "trust_score",
    "group_constraint",
]


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_service() RETURNS BOOLEAN LANGUAGE sql STABLE AS
        $$ SELECT current_setting('app.service', true) = 'on' $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_solver() RETURNS BOOLEAN LANGUAGE sql STABLE AS
        $$ SELECT current_setting('app.solver', true) = 'on' $$;
        """
    )

    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # ── group_constraint: the dignity guarantee ───────────────────────────────
    # A guest token carries app.group_id and app.slot. It can write its own row and read its
    # own row back. There is deliberately no policy for the group creator, for other members,
    # for admins, or for the service path.
    op.execute(
        """
        CREATE POLICY gc_self_read ON group_constraint FOR SELECT USING (
          group_id = app_group_id() AND member_slot = app_slot()
        );
        """
    )
    op.execute(
        """
        CREATE POLICY gc_self_write ON group_constraint FOR INSERT WITH CHECK (
          group_id = app_group_id() AND member_slot = app_slot()
        );
        """
    )
    op.execute(
        """
        CREATE POLICY gc_self_update ON group_constraint FOR UPDATE
          USING (group_id = app_group_id() AND member_slot = app_slot())
          WITH CHECK (group_id = app_group_id() AND member_slot = app_slot());
        """
    )
    op.execute(
        """
        CREATE POLICY gc_solver_read ON group_constraint FOR SELECT USING (
          app_solver() AND group_id = app_group_id()
        );
        """
    )

    # ── observation ───────────────────────────────────────────────────────────
    # No SELECT policy for any user role. The public sees live_state and aggregates; raw
    # observations are the estimator's input and nobody else's business.
    op.execute(
        """
        CREATE POLICY obs_staff_insert ON observation FOR INSERT WITH CHECK (
          source = 'staff' AND venue_id = app_venue_id()
        );
        """
    )
    op.execute(
        """
        CREATE POLICY obs_diner_insert ON observation FOR INSERT WITH CHECK (
          source = 'checkin' AND reporter_id = app_user_id()
        );
        """
    )
    op.execute(
        """
        CREATE POLICY obs_service_all ON observation FOR ALL
          USING (app_service()) WITH CHECK (app_service());
        """
    )

    # ── regulatory events ─────────────────────────────────────────────────────
    # No INSERT, UPDATE or DELETE policy for any user role. An owner cannot remove a record
    # about themselves; they answer it through regulatory_reply.
    op.execute("CREATE POLICY reg_public_read ON regulatory_event FOR SELECT USING (published)")
    op.execute(
        """
        CREATE POLICY reg_service_all ON regulatory_event FOR ALL
          USING (app_service()) WITH CHECK (app_service());
        """
    )

    # ── trust score ───────────────────────────────────────────────────────────
    # Computed, never set. There is no user write path by construction.
    op.execute("CREATE POLICY trust_public_read ON trust_score FOR SELECT USING (true)")
    op.execute(
        """
        CREATE POLICY trust_service_all ON trust_score FOR ALL
          USING (app_service()) WITH CHECK (app_service());
        """
    )

    # ── venue ─────────────────────────────────────────────────────────────────
    op.execute(
        "CREATE POLICY venue_public_read ON venue FOR SELECT USING (status <> 'hidden')"
    )
    op.execute(
        """
        CREATE POLICY venue_owner_update ON venue FOR UPDATE
          USING (claimed_by = app_user_id() AND app_role() = 'owner')
          WITH CHECK (claimed_by = app_user_id());
        """
    )
    op.execute(
        """
        CREATE POLICY venue_admin_all ON venue FOR ALL
          USING (app_role() = 'admin') WITH CHECK (app_role() = 'admin');
        """
    )
    op.execute(
        """
        CREATE POLICY venue_service_all ON venue FOR ALL
          USING (app_service()) WITH CHECK (app_service());
        """
    )

    # Column-level guard. RLS decides which rows an owner may update; it cannot say which
    # columns. Without this, `PATCH /owner/venues/{id}` with `{"tier": "live",
    # "google_rating": 5}` in the body would be a valid row-level update and the endpoint's
    # allow-list would be the only thing standing in the way. This puts the rule under the
    # endpoint rather than inside it.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION venue_guard_protected_columns() RETURNS TRIGGER
        LANGUAGE plpgsql AS $$
        BEGIN
          IF app_role() = 'admin' OR app_service() THEN
            RETURN NEW;
          END IF;
          NEW.id                  := OLD.id;
          NEW.place_id            := OLD.place_id;
          NEW.slug                := OLD.slug;
          NEW.tier                := OLD.tier;
          NEW.claimed_by          := OLD.claimed_by;
          NEW.claimed_at          := OLD.claimed_at;
          NEW.google_rating       := OLD.google_rating;
          NEW.google_review_count := OLD.google_review_count;
          NEW.rating_histogram    := OLD.rating_histogram;
          NEW.vibe_embedding      := OLD.vibe_embedding;
          NEW.created_at          := OLD.created_at;
          RETURN NEW;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER venue_guard_protected BEFORE UPDATE ON venue
        FOR EACH ROW EXECUTE FUNCTION venue_guard_protected_columns();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS venue_guard_protected ON venue")
    op.execute("DROP FUNCTION IF EXISTS venue_guard_protected_columns()")
    for policy, table in [
        ("gc_self_read", "group_constraint"),
        ("gc_self_write", "group_constraint"),
        ("gc_self_update", "group_constraint"),
        ("gc_solver_read", "group_constraint"),
        ("obs_staff_insert", "observation"),
        ("obs_diner_insert", "observation"),
        ("obs_service_all", "observation"),
        ("reg_public_read", "regulatory_event"),
        ("reg_service_all", "regulatory_event"),
        ("trust_public_read", "trust_score"),
        ("trust_service_all", "trust_score"),
        ("venue_public_read", "venue"),
        ("venue_owner_update", "venue"),
        ("venue_admin_all", "venue"),
        ("venue_service_all", "venue"),
    ]:
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION IF EXISTS app_solver()")
    op.execute("DROP FUNCTION IF EXISTS app_service()")
