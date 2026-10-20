"""Let the app role create next month's observation partition.

The monthly job calls `ensure_observation_partition` through a service session, which
connects as `haazir_app`. That role has DML only (0012) and no CREATE on the schema, so the
call failed with "permission denied for schema public" and the job had never actually made a
partition; 0005's thirteen months up front were all there was. Running the function as its
owner, with a fixed search_path, gives the job exactly this one DDL statement and nothing else.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER FUNCTION ensure_observation_partition(DATE) "
        "SECURITY DEFINER SET search_path = public, pg_temp"
    )
    op.execute("REVOKE ALL ON FUNCTION ensure_observation_partition(DATE) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION ensure_observation_partition(DATE) TO haazir_app")


def downgrade() -> None:
    op.execute("ALTER FUNCTION ensure_observation_partition(DATE) SECURITY INVOKER RESET search_path")
    op.execute("GRANT EXECUTE ON FUNCTION ensure_observation_partition(DATE) TO PUBLIC")
