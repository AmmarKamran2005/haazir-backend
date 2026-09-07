"""The application role. Plan §4: *the API must connect as a non-superuser role with RLS
enforced*.

**Why this migration exists.** Neon's default role, the one its console hands you, is a member
of `neon_superuser` and therefore carries the `rolbypassrls` attribute. `BYPASSRLS` outranks
`FORCE ROW LEVEL SECURITY`: a role that has it skips every policy on every table, and nothing
in the schema shows that it is happening. Connecting the API as that role means migration 0010
is decoration. This was not theoretical here; it was caught by nineteen failing tests that all
said the same thing, that anyone could read anything.

So there are two roles, with different jobs:

  hazir_owner  (Neon default)  owns the schema, runs migrations, has BYPASSRLS.
  haazir_app                   what the API and the workers connect as. NOBYPASSRLS,
                               owns nothing, holds only DML privileges.

`haazir_app` is created with SQL rather than through the Neon console or CLI, and that
distinction matters: a role Neon creates is granted `neon_superuser` and would inherit the
same bypass, reintroducing the bug through the door it was just closed. A role created here is
a plain Postgres role.

`FORCE` stays on the tables even though `haazir_app` does not own them. It costs nothing and
it means that if anything ever does connect as the owner, policies still apply.

Set `APP_DB_PASSWORD` before running this to set or rotate the login password. Without it the
role is still created and granted, just left unable to log in, so a migration never fails for
want of a secret.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "haazir_app"


def upgrade() -> None:
    conn = op.get_bind()

    op.execute(
        f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            CREATE ROLE {APP_ROLE} NOLOGIN NOCREATEDB NOCREATEROLE
                                   NOINHERIT NOREPLICATION NOBYPASSRLS;
          END IF;
        END $$;
        """
    )
    # Belt and braces: if the role already existed, from an earlier run or from the Neon
    # console, force the attributes that matter rather than trusting how it was made.
    #
    # NOSUPERUSER is not in this list on purpose. Only a superuser may set that attribute, and
    # Neon's owner role is not one; it merely has BYPASSRLS and CREATEROLE. Naming it here
    # fails the whole migration to assert a default that a non-superuser could not have
    # granted in the first place. The assertion that matters is NOBYPASSRLS, and changing
    # that requires BYPASSRLS, which the owner does have.
    op.execute(f"ALTER ROLE {APP_ROLE} NOBYPASSRLS NOCREATEROLE NOCREATEDB")
    # NOINHERIT above is the other half of the defence: even if something grants this role
    # membership of neon_superuser later, it will not pick the attribute up automatically.
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {APP_ROLE}")

    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    # DML only. No CREATE on the schema, no TRUNCATE, no REFERENCES: the application changes
    # rows, and changing the shape of the database is a migration's job.
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")

    # Everything a later migration creates, without having to remember to come back here.
    owner = conn.scalar(sa.text("SELECT current_user"))
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
    )

    # `observation` is partitioned. Grants on the parent do not reach partitions that already
    # exist, and 0005 created thirteen of them.
    op.execute(
        f"""
        DO $$
        DECLARE part TEXT;
        BEGIN
          FOR part IN
            SELECT c.relname FROM pg_inherits i
              JOIN pg_class c ON c.oid = i.inhrelid
              JOIN pg_class p ON p.oid = i.inhparent
             WHERE p.relname = 'observation'
          LOOP
            EXECUTE format(
              'GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO {APP_ROLE}', part);
          END LOOP;
        END $$;
        """
    )

    password = os.environ.get("APP_DB_PASSWORD", "").strip()
    if password:
        # `ALTER ROLE ... PASSWORD` is a utility statement and takes no bind parameters, so
        # the value has to arrive as a literal. Rather than escaping it in Python and hoping,
        # Postgres builds the statement itself with %L, which is what `quote_literal` uses.
        # The password never has to be trusted to contain no quotes.
        stmt = conn.scalar(
            # The cast is required: format() takes variadic "any", so without it asyncpg
            # cannot infer the parameter type and raises IndeterminateDatatypeError.
            sa.text(f"SELECT format('ALTER ROLE {APP_ROLE} LOGIN PASSWORD %L', CAST(:pw AS text))"),
            {"pw": password},
        )
        conn.exec_driver_sql(stmt)


def downgrade() -> None:
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE USAGE, SELECT ON SEQUENCES FROM {APP_ROLE}"
    )
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {APP_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {APP_ROLE}")
