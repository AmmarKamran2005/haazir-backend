"""The blanket write throttle. Plan §12 Phase 10, "rate limits everywhere".

The suite runs with `RATE_LIMIT_ENABLED=false`, because a per-minute ceiling applied to every
test would make the suite fail on how fast it runs rather than on what the code does. So this
file turns it on deliberately and tests the middleware directly.

The property worth protecting is not any particular number. It is that a write endpoint added
next month is covered without anybody remembering to cover it.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from haazir.auth import ratelimit, throttle

from .conftest import requires_db


@pytest.fixture
def throttled(monkeypatch):
    """The middleware switched on, with a clean window."""
    from haazir import config

    ratelimit.reset()
    monkeypatch.setattr(config.settings, "rate_limit_enabled", True)
    yield
    ratelimit.reset()


# --- the rules, no database ---------------------------------------------------


def test_every_write_path_gets_a_limit_even_if_it_matches_no_rule():
    """The whole point of doing this in middleware. An endpoint nobody wrote a rule for is
    still covered."""
    assert throttle.limit_for("/v1/some/endpoint/invented/tomorrow") == (
        throttle.DEFAULT_WRITE_LIMIT
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/search", 60),
        ("/v1/ask", 60),
        ("/v1/auth/request-link", 12),
        ("/v1/auth/verify", 30),
        ("/v1/groups", 10),
        ("/v1/venues/abc-123/facts", 20),
        ("/v1/admin/ingest/sfa", 120),
        ("/v1/checkin", throttle.DEFAULT_WRITE_LIMIT),
    ],
)
def test_the_configured_limits(path, expected):
    assert throttle.limit_for(path) == expected


def test_the_magic_link_endpoint_is_far_tighter_than_the_read_endpoints():
    """Without a low ceiling here, anybody can point this service at a stranger's inbox.

    Compared against the high-volume reads rather than against every write. `/v1/groups` is
    lower still at 10, and that is fine: an earlier version of this test asserted
    request-link was the tightest of all and failed on exactly that, which was the test
    committing to a ranking nobody had a reason to want.
    """
    for read_path in ("/v1/search", "/v1/ask"):
        assert throttle.limit_for("/v1/auth/request-link") < throttle.limit_for(read_path) / 2


def test_the_client_ip_comes_from_the_proxy_header():
    """Fly.io terminates TLS. Without reading its header every request looks like it came
    from the proxy and the limit protects nothing at all."""
    from starlette.datastructures import Headers
    from starlette.requests import Request

    def request_with(headers: dict) -> Request:
        scope = {
            "type": "http", "method": "POST", "path": "/v1/search",
            "headers": Headers(headers).raw, "client": ("10.0.0.1", 1234),
        }
        return Request(scope)

    assert throttle.client_ip(request_with({"fly-client-ip": "203.0.113.5"})) == "203.0.113.5"
    assert throttle.client_ip(
        request_with({"x-forwarded-for": "198.51.100.9, 10.0.0.1"})
    ) == "198.51.100.9"
    assert throttle.client_ip(request_with({})) == "10.0.0.1"


# --- the middleware in the app ------------------------------------------------


async def app_client():
    from haazir.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@requires_db
@pytest.mark.asyncio
async def test_a_flood_of_writes_is_throttled(clean_db, throttled):
    limit = throttle.limit_for("/v1/groups")
    async with await app_client() as c:
        codes = [
            (
                await c.post(
                    "/v1/groups", json={"title": "T", "members": ["A", "B"]},
                    headers={"fly-client-ip": "203.0.113.77"},
                )
            ).status_code
            for _ in range(limit + 3)
        ]

    assert 429 in codes
    assert codes.index(429) >= limit  # the first `limit` requests got through


@requires_db
@pytest.mark.asyncio
async def test_a_throttled_response_says_how_long_to_wait(clean_db, throttled):
    async with await app_client() as c:
        headers = {"fly-client-ip": "203.0.113.78"}
        last = None
        for _ in range(throttle.limit_for("/v1/groups") + 2):
            last = await c.post(
                "/v1/groups", json={"title": "T", "members": ["A", "B"]}, headers=headers
            )

    assert last.status_code == 429
    assert "Retry-After" in last.headers
    assert last.json()["limit_per_minute"] == throttle.limit_for("/v1/groups")


@requires_db
@pytest.mark.asyncio
async def test_two_addresses_do_not_share_a_budget(clean_db, throttled):
    """One noisy client must not lock everybody else out."""
    limit = throttle.limit_for("/v1/groups")
    body = {"title": "T", "members": ["A", "B"]}

    async with await app_client() as c:
        for _ in range(limit + 2):
            await c.post("/v1/groups", json=body, headers={"fly-client-ip": "203.0.113.80"})
        other = await c.post(
            "/v1/groups", json=body, headers={"fly-client-ip": "203.0.113.81"}
        )

    assert other.status_code != 429


@requires_db
@pytest.mark.asyncio
async def test_reads_are_never_throttled(clean_db, throttled):
    """Browsing is never gated (§5), and a cap on GETs would hit the city map polling for the
    live feed long before it hit anybody abusing the API."""
    async with await app_client() as c:
        codes = [
            (await c.get("/health", headers={"fly-client-ip": "203.0.113.90"})).status_code
            for _ in range(throttle.DEFAULT_WRITE_LIMIT * 2)
        ]
    assert set(codes) == {200}


@requires_db
@pytest.mark.asyncio
async def test_the_throttle_is_off_when_the_setting_says_so(clean_db):
    """The switch the suite itself relies on. If this stopped working, every other test would
    start failing on timing."""
    async with await app_client() as c:
        codes = [
            (
                await c.post(
                    "/v1/groups", json={"title": "T", "members": ["A", "B"]},
                    headers={"fly-client-ip": "203.0.113.99"},
                )
            ).status_code
            for _ in range(throttle.limit_for("/v1/groups") + 5)
        ]
    assert 429 not in codes
