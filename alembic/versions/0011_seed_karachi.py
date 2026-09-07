"""Seed Karachi: one city, twenty-four areas. Plan §12 Phase 1.

The list is lifted verbatim from `scraper/config.py:KARACHI_AREAS` so the ingestion pipeline
and the database agree on one canonical spelling per area. `docs/SCRAPING-PROMPT.md` §5 is
explicit that this has to be "DHA Phase 6" and never "Defence Phase 6"; if the two sides drift,
`area_id` resolution starts producing nulls and the city map quietly loses neighbourhoods.

`boundary` stays NULL. The scraper's rectangles are deliberately generous and overlapping,
because Text Search caps at 60 results and an over-tight rectangle loses venues. They are good
search windows and bad polygons: a venue in Zamzama falls inside the Clifton rectangle too, so
`ST_Contains` would have several right answers. Resolution therefore uses the nearest centroid,
which is the plan's stated fallback (§10.1 rule 2). Real boundaries can be added later and
containment will start winning on its own.

Idempotent. Re-running updates the centroid and leaves ids alone, so foreign keys survive.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, name_urdu, lng, lat) — lng first, because that is the order ST_MakePoint takes and
# swapping them is the single most common way to put a whole city in the Arabian Sea.
KARACHI_AREAS: list[tuple[str, str, float, float]] = [
    ("Do Darya", "دو دریا", 67.1477, 24.7906),
    ("Clifton", "کلفٹن", 67.03, 24.8138),
    ("Boat Basin", "بوٹ بیسن", 67.0335, 24.83),
    ("Zamzama", "زمزمہ", 67.0345, 24.8158),
    ("DHA Phase 2", "ڈی ایچ اے فیز ۲", 67.062, 24.8281),
    ("DHA Phase 5", "ڈی ایچ اے فیز ۵", 67.0555, 24.805),
    ("DHA Phase 6", "ڈی ایچ اے فیز ۶", 67.0735, 24.808),
    ("DHA Phase 8", "ڈی ایچ اے فیز ۸", 67.101, 24.8115),
    ("Tariq Road", "طارق روڈ", 67.0622, 24.872),
    ("Bahadurabad", "بہادرآباد", 67.064, 24.876),
    ("PECHS", "پی ای سی ایچ ایس", 67.068, 24.868),
    ("Burns Road", "برنس روڈ", 67.018, 24.8615),
    ("Saddar", "صدر", 67.029, 24.857),
    ("Shahrah-e-Faisal", "شاہراہِ فیصل", 67.1, 24.87),
    ("Gulshan-e-Iqbal", "گلشنِ اقبال", 67.09, 24.92),
    ("Gulistan-e-Johar", "گلستانِ جوہر", 67.13, 24.92),
    ("Federal B Area", "فیڈرل بی ایریا", 67.055, 24.93),
    ("North Nazimabad", "نارتھ ناظم آباد", 67.035, 24.95),
    ("Nazimabad", "ناظم آباد", 67.03, 24.91),
    ("Malir", "ملیر", 67.2, 24.89),
    ("Korangi", "کورنگی", 67.13, 24.84),
    ("Gulberg", "گلبرگ", 67.07, 24.94),
    ("Hyderi", "حیدری", 67.038, 24.944),
    ("Bahria Town", "بحریہ ٹاؤن", 67.32, 25.01),
]


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO city (name, name_urdu, country, timezone, centroid, is_active)
        VALUES ('Karachi', 'کراچی', 'PK', 'Asia/Karachi',
                ST_SetSRID(ST_MakePoint(67.0011, 24.8607), 4326)::geography, TRUE)
        ON CONFLICT (name) DO UPDATE
          SET name_urdu = EXCLUDED.name_urdu,
              centroid  = EXCLUDED.centroid,
              is_active = EXCLUDED.is_active;
        """
    )

    conn = op.get_bind()
    for name, urdu, lng, lat in KARACHI_AREAS:
        conn.execute(
            sa.text(
                """
                INSERT INTO area (city_id, name, name_urdu, centroid)
                SELECT c.id, :name, :urdu,
                       ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography
                FROM city c WHERE c.name = 'Karachi'
                ON CONFLICT (city_id, name) DO UPDATE
                  SET name_urdu = EXCLUDED.name_urdu,
                      centroid  = EXCLUDED.centroid
                """
            ),
            {"name": name, "urdu": urdu, "lng": lng, "lat": lat},
        )


def downgrade() -> None:
    op.execute("DELETE FROM area WHERE city_id IN (SELECT id FROM city WHERE name = 'Karachi')")
    op.execute("DELETE FROM city WHERE name = 'Karachi'")
