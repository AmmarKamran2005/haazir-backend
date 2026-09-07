"""City and area. Plan §3.2.

`area.boundary` is nullable on purpose. Karachi's neighbourhood boundaries are not published
as clean polygons anywhere, so ingestion resolves an area by `ST_Contains` where a boundary
exists and by nearest centroid where it does not (§10.1 rule 2). Requiring the polygon up
front would have blocked the whole dataset on a mapping exercise nobody has finished.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE city (
          id          SMALLSERIAL PRIMARY KEY,
          name        TEXT NOT NULL UNIQUE,
          name_urdu   TEXT,
          country     TEXT NOT NULL DEFAULT 'PK',
          timezone    TEXT NOT NULL DEFAULT 'Asia/Karachi',
          centroid    GEOGRAPHY(POINT,4326) NOT NULL,
          is_active   BOOLEAN NOT NULL DEFAULT TRUE
        );
        """
    )
    op.execute(
        """
        CREATE TABLE area (
          id          SERIAL PRIMARY KEY,
          city_id     SMALLINT NOT NULL REFERENCES city(id),
          name        TEXT NOT NULL,
          name_urdu   TEXT,
          centroid    GEOGRAPHY(POINT,4326) NOT NULL,
          boundary    GEOGRAPHY(POLYGON,4326),
          CONSTRAINT area_city_id_name_key UNIQUE (city_id, name)
        );
        """
    )
    op.execute("CREATE INDEX area_centroid_gix ON area USING GIST (centroid)")
    op.execute("CREATE INDEX area_boundary_gix ON area USING GIST (boundary)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS area")
    op.execute("DROP TABLE IF EXISTS city")
