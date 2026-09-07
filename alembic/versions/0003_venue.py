"""Venue and provenance. Plan §3.3, §3.4.

`claimed_by` is created without its foreign key. `app_user` does not exist yet and the plan
adds the constraint in §3.8; migration 0007 does the same.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE venue (
          id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          place_id            TEXT UNIQUE,
          slug                TEXT UNIQUE NOT NULL,
          name                TEXT NOT NULL,
          name_urdu           TEXT,
          aliases             TEXT[] NOT NULL DEFAULT '{}',
          brand               TEXT,
          branch_label        TEXT,
          city_id             SMALLINT NOT NULL REFERENCES city(id),
          area_id             INT REFERENCES area(id),
          geom                GEOGRAPHY(POINT,4326) NOT NULL,
          address_full        TEXT,
          phone               TEXT,
          whatsapp            TEXT,
          website             TEXT,
          instagram           TEXT,
          maps_url            TEXT,
          venue_type          TEXT NOT NULL,
          cuisines            TEXT[] NOT NULL DEFAULT '{}',
          price_level         SMALLINT,
          avg_ticket_pkr      INT,
          capacity_covers     INT,
          google_rating       REAL,
          google_review_count INT,
          rating_histogram    JSONB,
          hours               JSONB,
          hours_ramadan       JSONB,
          popular_times       JSONB,
          attributes          JSONB NOT NULL DEFAULT '{}'::jsonb,
          photos              JSONB NOT NULL DEFAULT '[]'::jsonb,
          blurb               TEXT,
          tier                venue_tier   NOT NULL DEFAULT 'seeded',
          status              venue_status NOT NULL DEFAULT 'active',
          claimed_by          UUID,
          claimed_at          TIMESTAMPTZ,
          vibe_embedding      VECTOR(1024),
          created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT venue_price_level_check CHECK (price_level BETWEEN 1 AND 4)
        );
        """
    )

    op.execute("CREATE INDEX venue_geom_gix  ON venue USING GIST (geom)")
    op.execute("CREATE INDEX venue_name_trgm ON venue USING GIN (name gin_trgm_ops)")
    op.execute("CREATE INDEX venue_attrs_gin ON venue USING GIN (attributes jsonb_path_ops)")
    op.execute("CREATE INDEX venue_cuisines  ON venue USING GIN (cuisines)")
    op.execute(
        "CREATE INDEX venue_city_tier ON venue (city_id, tier) WHERE status = 'active'"
    )
    # HNSW is built at load time, so it is created here empty and cheap. Building it after
    # 1,200 venues and their embeddings are already in place costs minutes instead.
    op.execute(
        "CREATE INDEX venue_vibe_hnsw ON venue USING hnsw (vibe_embedding vector_cosine_ops)"
    )

    op.execute(
        """
        CREATE TABLE venue_source (
          id           BIGSERIAL PRIMARY KEY,
          venue_id     UUID NOT NULL REFERENCES venue(id) ON DELETE CASCADE,
          source       TEXT NOT NULL,
          source_url   TEXT,
          scraped_at   TIMESTAMPTZ NOT NULL,
          confidence   REAL NOT NULL DEFAULT 1.0,
          payload      JSONB
        );
        """
    )
    op.execute("CREATE INDEX venue_source_venue ON venue_source (venue_id, scraped_at DESC)")

    # Keeps `updated_at` honest without every writer having to remember it.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER
        LANGUAGE plpgsql AS $$
        BEGIN
          NEW.updated_at := now();
          RETURN NEW;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER venue_touch_updated_at BEFORE UPDATE ON venue
        FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS venue_touch_updated_at ON venue")
    op.execute("DROP FUNCTION IF EXISTS touch_updated_at()")
    op.execute("DROP TABLE IF EXISTS venue_source")
    op.execute("DROP TABLE IF EXISTS venue")
