"""Repair facts the nightly decay compounded into the floor. §3.4.

`recompute.decay_facts` used to multiply each fact's current confidence by
`exp(-days_since_verified / 180)` every night while `at` stayed the verification date. That
compounds: night d applied `exp(-d/180)` on top of all the earlier nights, so the confidence
was `c0 * exp(-d(d+1)/360)` instead of `c0 * exp(-d/180)`. A scraped fact (0.75) crossed the
0.5 floor the hard filters use after about twelve nights rather than about seventy-three, and
reached the 0.25 floor after about nineteen.

The visible symptom was `needs_card`, `needs_ramp` and the other access filters returning no
venues at all in production. Checked before this migration was written: all 16,667 facts in
the production `venue.attributes` were `places_api` facts dated 2026-09-03, every one of them
at exactly 0.25, none above the 0.5 floor, and there were no `fact_verification` rows.

The code fix is to decay from the confidence the fact was verified at, kept alongside as
`c0`. This migration puts that base in place and restores the damage:

* a `places_api` fact was ingested at 0.75, so it gets `c0 = 0.75` and a `c` recomputed from
  its real age. `0.75` is written out rather than imported, because a migration describes
  the data as it was and must not change meaning when a constant in the app does.
* any other fact (a `diner_verified` one) keeps the `c` it has and takes it as `c0`. That is
  no worse than before and cannot be repaired from the fact alone; the production database
  has none, and the next verification rewrites the fact from `fact_verification` anyway.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE venue SET attributes = (
            SELECT jsonb_object_agg(
                key,
                CASE
                    WHEN value->>'src' = 'places_api' AND value ? 'at' AND value ? 'c'
                    THEN value || jsonb_build_object(
                        'c0', 0.75,
                        'c', GREATEST(0.25, 0.75 *
                             exp(-GREATEST(0, (CURRENT_DATE - (value->>'at')::date)) / 180.0)))
                    WHEN value ? 'c' AND NOT value ? 'c0'
                    THEN value || jsonb_build_object('c0', (value->>'c')::float)
                    ELSE value
                END)
              FROM jsonb_each(attributes)
        )
        WHERE attributes <> '{}'::jsonb
        """
    )


def downgrade() -> None:
    # The restored confidences stay: putting the compounded values back would reinstate the
    # outage. Only the new key goes.
    op.execute(
        """
        UPDATE venue SET attributes = (
            SELECT jsonb_object_agg(key, value - 'c0') FROM jsonb_each(attributes)
        )
        WHERE attributes <> '{}'::jsonb
        """
    )
