"""Test fixtures.

Tests split in two. The ones that need no database run anywhere; the ones that do are skipped
with a visible reason when no test database is configured, rather than failing and burying
the signal from the tests that did run.

Isolation is by truncation between tests rather than by a rolled-back outer transaction,
because several code paths commit deliberately: a magic link is committed before the email is
sent, a rejected geofenced observation is committed before its 409. Wrapping those in a
savepoint would test something other than what ships.

**`clean_db` runs `TRUNCATE` on real tables, so the database it points at is chosen
deliberately and never by default.** The suite reads `TEST_DATABASE_URL` and refuses to fall
back to `DATABASE_URL`. Running `pytest` with a normal `.env` present therefore skips the
database tests instead of wiping the development data, which is the failure this arrangement
exists to prevent: on Neon a branch is instant and free, and losing a scraped dataset to a
stray test run is not.
"""

from __future__ import annotations

import os
import pathlib
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

ENV_FILE = pathlib.Path(__file__).resolve().parents[1] / ".env"


def _from_env_file(key: str) -> str | None:
    """Read one key out of `api/.env`.

    The test URLs live in the same file as everything else so there is one place to configure,
    but they are deliberately not loaded into the settings object: `TEST_DATABASE_URL` has to
    be an explicit opt-in, not something the app could ever pick up on its own.
    """
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'") or None
    return None


TEST_DB_URL = os.environ.get("TEST_DATABASE_URL") or _from_env_file("TEST_DATABASE_URL")
TEST_DB_URL_DIRECT = (
    os.environ.get("TEST_DATABASE_URL_DIRECT")
    or _from_env_file("TEST_DATABASE_URL_DIRECT")
    or TEST_DB_URL
)

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-the-guard-check-x")
os.environ.setdefault("COOKIE_SECURE", "false")
# The suite drives the jobs directly where it needs them. A scheduler firing every sixty
# seconds through a fourteen-minute run would rewrite live_state under tests that are
# asserting on it.
os.environ.setdefault("RUN_SCHEDULER", "false")
# The blanket write throttle is off here for the same reason: a test that submits six
# group constraints in a second is not abuse, and a per-minute ceiling would make the
# suite fail on speed rather than on behaviour. `test_throttle.py` covers it directly.
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
# Overwrite, not setdefault. `api/.env` carries a real Gemini key, and without this the suite
# would make live, billed calls to a third party — slow, chargeable, and dependent on someone
# else's uptime for a green build. The no-key path is also the one worth testing by default,
# because it is what ships when the budget guard trips. The handful of tests that need a key
# present monkeypatch it.
os.environ["GEMINI_API_KEY"] = ""

if TEST_DB_URL:
    # Overwrite, not setdefault. An environment variable beats the .env file in
    # pydantic-settings, so this is what guarantees the suite cannot reach the development
    # database even when `.env` names one.
    os.environ["DATABASE_URL"] = TEST_DB_URL
    os.environ["DATABASE_URL_DIRECT"] = TEST_DB_URL_DIRECT or TEST_DB_URL
else:
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("DATABASE_URL_DIRECT", None)

HAS_DB = bool(TEST_DB_URL)
requires_db = pytest.mark.skipif(
    HAS_DB is False,
    reason="TEST_DATABASE_URL is not set (point it at a throwaway Neon branch; the suite "
    "truncates every table)",
)

# Order matters: children first, so the cascades have nothing left to do.
WIPE = [
    "observation",
    "group_solution",
    "group_constraint",
    "group_member",
    "group_token",
    "group_session",
    "device_token",
    "refresh_token",
    "magic_link",
    "attribution",
    "visit",
    "fact_verification",
    "live_state",
    "occupancy_prior",
    "source_calibration",
    "trust_score",
    "regulatory_reply",
    "regulatory_event",
    "review_sample",
    "dish_time_quality",
    "dish_availability",
    "venue_dish_price",
    "venue_dish",
    "venue_source",
    "offer",
    "venue",
    "app_user",
]


_owner_engine = None


def owner_engine():
    """A second engine on the schema owner, for the one thing the app role cannot do.

    `haazir_app` is granted SELECT, INSERT, UPDATE and DELETE and nothing else. It has no
    TRUNCATE, deliberately: emptying a table is not an application operation. Test cleanup is
    an operator action, so it arrives as the operator.

    Sharing one engine across the session matters as much here as it does in `db.py`: an
    asyncpg connection belongs to the loop that opened it, and the suite runs on one loop.
    """
    global _owner_engine
    if _owner_engine is None:
        from haazir.config import _to_asyncpg
        from haazir.db import make_engine

        _owner_engine = make_engine(_to_asyncpg(TEST_DB_URL_DIRECT))
    return _owner_engine


@pytest_asyncio.fixture
async def clean_db():
    if not HAS_DB:
        pytest.skip("TEST_DATABASE_URL is not set")

    async with owner_engine().begin() as conn:
        # TRUNCATE takes an ACCESS EXCLUSIVE lock on every table in the list. If any other
        # backend is sitting idle in a transaction that touched one of them, this waits for
        # that transaction to end, and with no timeout it waits indefinitely. One run of this
        # suite spent two hours inside this statement before anything reported it. Ten
        # seconds turns that into a clear error naming the lock, which is the useful outcome.
        await conn.execute(text("SET LOCAL lock_timeout = '10s'"))
        await conn.execute(text("SET LOCAL statement_timeout = '60s'"))
        await conn.execute(text(f"TRUNCATE {', '.join(WIPE)} RESTART IDENTITY CASCADE"))
    yield
    from haazir.auth import ratelimit

    ratelimit.reset()


class RoundTrips:
    """Counts statements sent to the database inside a `with` block.

    Wall-clock latency is not a portable assertion: from Karachi one round trip to Neon in
    Singapore is about 170 ms, and co-located it is about 1 ms, so the same code passes or
    fails a millisecond budget depending on where the test runs. The count does not move, and
    it is what actually regresses when somebody adds a query to a hot path.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __len__(self) -> int:
        return len(self.statements)

    def __repr__(self) -> str:
        return f"<{len(self)} round trips>"


@pytest.fixture
def count_round_trips():
    """`with count_round_trips() as trips: ...` then assert on `len(trips)`."""
    import contextlib

    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    @contextlib.contextmanager
    def counter():
        trips = RoundTrips()

        def before(conn, cursor, statement, params, context, executemany):
            first_line = statement.strip().splitlines()[0]
            trips.statements.append(first_line[:90])

        event.listen(Engine, "before_cursor_execute", before)
        try:
            yield trips
        finally:
            event.remove(Engine, "before_cursor_execute", before)

    return counter


@pytest_asyncio.fixture
async def client(clean_db):
    from haazir.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def karachi_city_id():
    from haazir.db import service_session

    async with service_session() as s:
        city_id = await s.scalar(text("SELECT id FROM city WHERE name = 'Karachi'"))
    if city_id is None:
        pytest.skip("Karachi is not seeded; run `alembic upgrade head`")
    return city_id


@pytest_asyncio.fixture
async def venue_factory(karachi_city_id):
    """Creates venues at a known point so geofence distances are predictable.

    Default location is Burns Road (67.0180, 24.8615), which is a real place and keeps the
    numbers in the test readable as metres from somewhere.
    """
    from haazir.db import service_session

    async def make(
        name: str = "Test Venue",
        lat: float = 24.8615,
        lng: float = 67.0180,
        claimed_by: uuid.UUID | None = None,
        status: str = "active",
    ) -> uuid.UUID:
        slug = f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}"
        async with service_session() as s:
            return await s.scalar(
                text(
                    """
                    INSERT INTO venue (slug, name, city_id, geom, venue_type,
                                       claimed_by, status)
                    VALUES (:slug, :name, :city,
                            ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
                            'restaurant', :claimed_by, CAST(:status AS venue_status))
                 RETURNING id
                    """
                ),
                {
                    "slug": slug,
                    "name": name,
                    "city": karachi_city_id,
                    "lat": lat,
                    "lng": lng,
                    "claimed_by": claimed_by,
                    "status": status,
                },
            )

    return make


@pytest_asyncio.fixture
async def user_factory():
    from haazir.db import service_session

    async def make(email: str | None = None, role: str = "diner") -> tuple[uuid.UUID, str]:
        email = email or f"{uuid.uuid4().hex[:10]}@example.com"
        async with service_session() as s:
            uid = await s.scalar(
                text(
                    """
                    INSERT INTO app_user (email, email_verified, role)
                    VALUES (:email, TRUE, CAST(:role AS user_role))
                 RETURNING id
                    """
                ),
                {"email": email, "role": role},
            )
        return uid, email

    return make
