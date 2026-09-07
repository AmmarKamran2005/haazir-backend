"""Phase 1 acceptance. Plan §12.

*`alembic upgrade head` on a fresh Neon branch creates every table; `/health` returns green;
`SELECT postgis_version()` and `vector` both work.*
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from haazir.db import Base, service_session

from .conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


async def test_every_mapped_table_exists_in_the_database():
    """Catches the reverse of the usual drift: a model that no migration ever created."""
    async with service_session() as s:
        rows = await s.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        present = {r[0] for r in rows}
    missing = sorted(set(Base.metadata.tables) - present)
    assert missing == [], f"mapped but not migrated: {missing}"


async def test_the_schema_has_the_thirty_tables_the_plan_describes():
    async with service_session() as s:
        rows = await s.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename NOT LIKE 'observation_%' "
                "AND tablename NOT IN ('alembic_version', 'spatial_ref_sys')"
            )
        )
        present = {r[0] for r in rows}
    assert len(present) == 30, sorted(present)


async def test_postgis_and_pgvector_are_installed_and_usable():
    async with service_session() as s:
        version = await s.scalar(text("SELECT postgis_version()"))
        assert version

        # Not just present: actually computing. Burns Road to Do Darya is about 13 km.
        metres = await s.scalar(
            text(
                "SELECT ST_Distance("
                "  ST_SetSRID(ST_MakePoint(67.0180, 24.8615), 4326)::geography,"
                "  ST_SetSRID(ST_MakePoint(67.1477, 24.7906), 4326)::geography)"
            )
        )
        assert 12_000 < metres < 18_000

        similarity = await s.scalar(
            text("SELECT '[1,0,0]'::vector <=> '[0,1,0]'::vector")
        )
        assert similarity == pytest.approx(1.0)


async def test_trigram_and_citext_work():
    async with service_session() as s:
        score = await s.scalar(text("SELECT similarity('Bihari Boti', 'Behari boti')"))
        assert score > 0.4  # the fuzzy venue and dish matching depends on this
        # Same address, different case. The domain swap that removed .test from the suite
        # once left these two literals as genuinely different addresses, and citext was
        # blamed for a failure that was a typo.
        assert await s.scalar(
            text("SELECT 'Ammar@Example.COM'::citext = 'ammar@example.com'::citext")
        )


async def test_karachi_and_its_areas_are_seeded():
    async with service_session() as s:
        city = (
            await s.execute(
                text("SELECT id, name, timezone FROM city WHERE name = 'Karachi'")
            )
        ).mappings().first()
        assert city is not None
        assert city["timezone"] == "Asia/Karachi"

        n = await s.scalar(
            text("SELECT count(*) FROM area WHERE city_id = :c"), {"c": city["id"]}
        )
    assert n == 24


async def test_area_names_match_the_scraper_spelling():
    """`docs/SCRAPING-PROMPT.md` §5: one canonical spelling per area. If the ingestion side
    and the database drift, `area_id` resolution starts returning nulls and the city map
    quietly loses neighbourhoods."""
    async with service_session() as s:
        rows = await s.execute(text("SELECT name FROM area ORDER BY name"))
        names = {r[0] for r in rows}
    assert "DHA Phase 6" in names
    assert "Defence Phase 6" not in names
    assert {"Burns Road", "Bahadurabad", "Gulshan-e-Iqbal", "Do Darya"} <= names


async def test_area_centroids_are_lng_lat_and_not_swapped():
    """Swapping the arguments to ST_MakePoint puts the whole city in the Arabian Sea, and
    nothing else in the system notices."""
    async with service_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT ST_Y(centroid::geometry) AS lat, ST_X(centroid::geometry) AS lng "
                    "FROM area WHERE name = 'Burns Road'"
                )
            )
        ).mappings().one()
    assert 24.7 < row["lat"] < 25.2
    assert 66.8 < row["lng"] < 67.5


async def test_observation_is_partitioned_with_runway():
    """An insert with no matching partition raises, and the insert path is the staff console
    during a live demo."""
    async with service_session() as s:
        partitions = await s.scalar(
            text(
                "SELECT count(*) FROM pg_inherits i "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE p.relname = 'observation'"
            )
        )
        assert partitions >= 12

        this_month = await s.scalar(
            text(
                "SELECT count(*) FROM pg_class "
                "WHERE relname = 'observation_' || to_char(now(), 'YYYY_MM')"
            )
        )
    assert this_month == 1


async def test_the_partition_helper_is_idempotent():
    async with service_session() as s:
        first = await s.scalar(
            text("SELECT ensure_observation_partition((now() + INTERVAL '18 months')::date)")
        )
        second = await s.scalar(
            text("SELECT ensure_observation_partition((now() + INTERVAL '18 months')::date)")
        )
    assert first == second


async def test_the_claim_helpers_read_the_transaction_settings():
    import uuid

    from haazir.db import Claims, session_scope

    uid = uuid.uuid4()
    async with session_scope(Claims(role="owner", user_id=uid)) as s:
        assert await s.scalar(text("SELECT app_role()")) == "owner"
        assert await s.scalar(text("SELECT app_user_id()")) == uid
        assert await s.scalar(text("SELECT app_service()")) is False
        assert await s.scalar(text("SELECT app_solver()")) is False

    async with session_scope() as s:
        assert await s.scalar(text("SELECT app_role()")) == "anon"
        assert await s.scalar(text("SELECT app_user_id()")) is None


async def test_health_db_reports_green(client):
    r = await client.get("/health/db")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["extensions"]["postgis"]
    assert body["extensions"]["vector"]
