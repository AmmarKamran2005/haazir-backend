"""Dishes, menus, price history, the Dish-Time Graph. Plan §3.5.

Two foreign keys are added beyond the plan's DDL: `dish_time_quality` and `dish_availability`
both reference `venue_dish` with ON DELETE CASCADE. Both tables are rebuilt by nightly jobs
that delete and reinsert. Without the cascade, removing a dish from a venue's menu leaves its
quality curve behind, and the venue page then renders a graph for a dish that is no longer
served. That is a wrong answer rather than a missing one, which is the failure this product
is specifically trying not to have.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE dish (
          id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          canonical_name  TEXT NOT NULL,
          name_normalized TEXT NOT NULL UNIQUE,
          name_urdu       TEXT,
          category        TEXT,
          heat_level      SMALLINT,
          ingredients     TEXT[] NOT NULL DEFAULT '{}',
          allergens       TEXT[] NOT NULL DEFAULT '{}',
          dietary_flags   TEXT[] NOT NULL DEFAULT '{}',
          embedding       VECTOR(1024),
          CONSTRAINT dish_heat_level_check CHECK (heat_level BETWEEN 0 AND 5)
        );
        """
    )
    op.execute("CREATE INDEX dish_norm_trgm ON dish USING GIN (name_normalized gin_trgm_ops)")

    op.execute(
        """
        CREATE TABLE venue_dish (
          venue_id        UUID NOT NULL REFERENCES venue(id) ON DELETE CASCADE,
          dish_id         UUID NOT NULL REFERENCES dish(id),
          menu_name       TEXT NOT NULL,
          section         TEXT,
          description     TEXT,
          price_pkr       INT,
          price_unit      price_unit NOT NULL DEFAULT 'per_plate',
          price_seen_at   TIMESTAMPTZ,
          price_source    TEXT,
          quality_mean    REAL,
          quality_n       INT NOT NULL DEFAULT 0,
          is_signature    BOOLEAN NOT NULL DEFAULT FALSE,
          sold_out_until  TIMESTAMPTZ,
          PRIMARY KEY (venue_id, dish_id)
        );
        """
    )
    op.execute("CREATE INDEX venue_dish_dish ON venue_dish (dish_id)")
    # The price-comparison endpoint filters to priced rows and sorts by price. Without this
    # it is a sequential scan of every menu line in the city.
    op.execute(
        "CREATE INDEX venue_dish_priced ON venue_dish (dish_id, price_pkr) "
        "WHERE price_pkr IS NOT NULL"
    )

    op.execute(
        """
        CREATE TABLE venue_dish_price (
          id          BIGSERIAL PRIMARY KEY,
          venue_id    UUID NOT NULL,
          dish_id     UUID NOT NULL,
          price_pkr   INT NOT NULL,
          seen_at     TIMESTAMPTZ NOT NULL,
          source      TEXT NOT NULL,
          FOREIGN KEY (venue_id, dish_id)
            REFERENCES venue_dish(venue_id, dish_id) ON DELETE CASCADE
        );
        """
    )
    op.execute("CREATE INDEX vdp_lookup ON venue_dish_price (dish_id, seen_at DESC)")

    op.execute(
        """
        CREATE TABLE dish_time_quality (
          venue_id       UUID NOT NULL,
          dish_id        UUID NOT NULL,
          hour_of_week   SMALLINT NOT NULL,
          quality        REAL NOT NULL,
          confidence     REAL NOT NULL,
          n_observations INT NOT NULL DEFAULT 0,
          PRIMARY KEY (venue_id, dish_id, hour_of_week),
          CONSTRAINT dtq_how_check CHECK (hour_of_week BETWEEN 0 AND 167),
          FOREIGN KEY (venue_id, dish_id)
            REFERENCES venue_dish(venue_id, dish_id) ON DELETE CASCADE
        );
        """
    )

    op.execute(
        """
        CREATE TABLE dish_availability (
          venue_id               UUID NOT NULL,
          dish_id                UUID NOT NULL,
          weekday                SMALLINT NOT NULL,
          typical_sellout_minute SMALLINT,
          sellout_sigma          REAL,
          PRIMARY KEY (venue_id, dish_id, weekday),
          CONSTRAINT dish_avail_weekday_check CHECK (weekday BETWEEN 0 AND 6),
          FOREIGN KEY (venue_id, dish_id)
            REFERENCES venue_dish(venue_id, dish_id) ON DELETE CASCADE
        );
        """
    )


def downgrade() -> None:
    for t in (
        "dish_availability",
        "dish_time_quality",
        "venue_dish_price",
        "venue_dish",
        "dish",
    ):
        op.execute(f"DROP TABLE IF EXISTS {t}")
