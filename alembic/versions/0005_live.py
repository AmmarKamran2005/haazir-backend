"""Live state. Plan §3.6.

`observation` is partitioned monthly by `observed_at`. Two consequences worth stating.

A partitioned table's primary key must contain the partition key, which is why the plan uses
`PRIMARY KEY (id, observed_at)` rather than `id` alone. Any query that fetches a single
observation must carry a time predicate or it fans out across every partition.

An insert with no matching partition raises `check_violation`, and the insert path here is the
staff console during a live demo. Rather than a DEFAULT partition, which then blocks creating
the real partition for that month, this migration provisions thirteen months up front and
`workers/scheduler.py` extends the runway monthly. If the scheduler stops, there is a year of
headroom before anything breaks.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE observation (
          id             BIGSERIAL,
          venue_id       UUID NOT NULL,
          source         obs_source NOT NULL,
          observed_at    TIMESTAMPTZ NOT NULL,
          value          REAL NOT NULL,
          sigma          REAL NOT NULL,
          reporter_id    UUID,
          device_id      UUID,
          reporter_trust REAL NOT NULL DEFAULT 0.5,
          geo_ok         BOOLEAN,
          payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
          PRIMARY KEY (id, observed_at),
          CONSTRAINT observation_value_check CHECK (value BETWEEN 0 AND 1),
          CONSTRAINT observation_sigma_check CHECK (sigma > 0)
        ) PARTITION BY RANGE (observed_at);
        """
    )
    op.execute("CREATE INDEX observation_venue_time ON observation (venue_id, observed_at DESC)")

    # Idempotent so both this migration and the monthly job can call it.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ensure_observation_partition(p_month DATE)
        RETURNS TEXT LANGUAGE plpgsql AS $$
        DECLARE
          start_at DATE := date_trunc('month', p_month)::date;
          end_at   DATE := (date_trunc('month', p_month) + INTERVAL '1 month')::date;
          part     TEXT := 'observation_' || to_char(start_at, 'YYYY_MM');
        BEGIN
          IF to_regclass(part) IS NULL THEN
            EXECUTE format(
              'CREATE TABLE %I PARTITION OF observation FOR VALUES FROM (%L) TO (%L)',
              part, start_at, end_at
            );
          END IF;
          RETURN part;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        DECLARE i INT;
        BEGIN
          FOR i IN -1..11 LOOP
            PERFORM ensure_observation_partition(
              (date_trunc('month', now()) + (i || ' month')::interval)::date
            );
          END LOOP;
        END $$;
        """
    )

    op.execute(
        """
        CREATE TABLE live_state (
          venue_id       UUID PRIMARY KEY REFERENCES venue(id) ON DELETE CASCADE,
          occupancy      REAL NOT NULL,
          sd             REAL NOT NULL,
          wait_p50_min   REAL NOT NULL,
          wait_p90_min   REAL NOT NULL,
          trend          REAL NOT NULL DEFAULT 0,
          confidence     REAL NOT NULL,
          band           TEXT NOT NULL,
          source_weights JSONB NOT NULL,
          updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT live_state_band_check
            CHECK (band IN ('free','moderate','busy','full'))
        );
        """
    )
    # The city map reads "who is busy right now" across the whole dataset every few seconds.
    op.execute("CREATE INDEX live_state_band ON live_state (band, updated_at DESC)")

    op.execute(
        """
        CREATE TABLE occupancy_prior (
          venue_id     UUID NOT NULL REFERENCES venue(id) ON DELETE CASCADE,
          hour_of_week SMALLINT NOT NULL,
          mean_ratio   REAL NOT NULL,
          sigma        REAL NOT NULL,
          source       TEXT NOT NULL,
          PRIMARY KEY (venue_id, hour_of_week),
          CONSTRAINT prior_how_check CHECK (hour_of_week BETWEEN 0 AND 167)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE source_calibration (
          venue_id UUID NOT NULL REFERENCES venue(id) ON DELETE CASCADE,
          source   obs_source NOT NULL,
          bias     REAL NOT NULL DEFAULT 0,
          variance REAL NOT NULL DEFAULT 0.04,
          trust    REAL NOT NULL DEFAULT 0.5,
          n        INT  NOT NULL DEFAULT 0,
          PRIMARY KEY (venue_id, source)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS source_calibration")
    op.execute("DROP TABLE IF EXISTS occupancy_prior")
    op.execute("DROP TABLE IF EXISTS live_state")
    op.execute("DROP TABLE IF EXISTS observation")  # cascades to every partition
    op.execute("DROP FUNCTION IF EXISTS ensure_observation_partition(DATE)")
