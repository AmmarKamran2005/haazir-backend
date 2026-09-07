"""Trust. Plan §3.7.

`review_sample` has no text column, which is deliberate rather than an omission: features and
an embedding are kept, the prose is dropped.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE regulatory_event (
          id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          venue_id         UUID REFERENCES venue(id) ON DELETE SET NULL,
          authority        TEXT NOT NULL,
          event_type       reg_event_type NOT NULL,
          event_date       DATE NOT NULL,
          reason           TEXT,
          fine_pkr         INT,
          source_url       TEXT NOT NULL,
          source_name      TEXT NOT NULL,
          raw_venue_name   TEXT NOT NULL,
          match_confidence REAL NOT NULL,
          published        BOOLEAN NOT NULL DEFAULT FALSE,
          reviewed_by      UUID,
          reviewed_at      TIMESTAMPTZ,
          created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT reg_event_confidence_check CHECK (match_confidence BETWEEN 0 AND 1)
        );
        """
    )
    op.execute("CREATE INDEX reg_event_venue ON regulatory_event (venue_id, event_date DESC)")

    # §10.2: the 0.90 threshold is the difference between a public record and a defamatory
    # claim against an innocent business, so it is enforced by the database and not only by
    # the ingestion code that happens to write these rows today.
    op.execute(
        """
        ALTER TABLE regulatory_event ADD CONSTRAINT reg_event_publish_threshold
        CHECK (NOT published OR match_confidence >= 0.90);
        """
    )

    op.execute(
        """
        CREATE TABLE regulatory_reply (
          id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          event_id   UUID NOT NULL REFERENCES regulatory_event(id) ON DELETE CASCADE,
          author_id  UUID NOT NULL,
          body       TEXT NOT NULL,
          published  BOOLEAN NOT NULL DEFAULT TRUE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX reg_reply_event ON regulatory_reply (event_id, created_at)")

    op.execute(
        """
        CREATE TABLE trust_score (
          venue_id    UUID PRIMARY KEY REFERENCES venue(id) ON DELETE CASCADE,
          score       SMALLINT NOT NULL,
          components  JSONB NOT NULL,
          computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT trust_score_range_check CHECK (score BETWEEN 0 AND 100)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE review_sample (
          id             BIGSERIAL PRIMARY KEY,
          venue_id       UUID NOT NULL REFERENCES venue(id) ON DELETE CASCADE,
          external_id    TEXT,
          rating         SMALLINT NOT NULL,
          lang           TEXT,
          posted_at      TIMESTAMPTZ,
          author_hash    TEXT NOT NULL,
          is_local_guide BOOLEAN,
          photo_count    SMALLINT,
          embedding      VECTOR(1024),
          flagged        BOOLEAN NOT NULL DEFAULT FALSE,
          flag_reason    TEXT,
          CONSTRAINT review_sample_rating_check CHECK (rating BETWEEN 1 AND 5),
          CONSTRAINT review_sample_venue_external_key UNIQUE (venue_id, external_id)
        );
        """
    )
    # One author posting many reviews for one venue is the cheapest review-fraud signal there
    # is, and this index is what makes checking for it a lookup rather than a scan.
    op.execute("CREATE INDEX review_sample_author ON review_sample (author_hash, venue_id)")


def downgrade() -> None:
    for t in ("review_sample", "trust_score", "regulatory_reply", "regulatory_event"):
        op.execute(f"DROP TABLE IF EXISTS {t}")
