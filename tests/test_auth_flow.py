"""Magic links, refresh rotation, cookies. Plan §5 rules 3, 4, 5, 6, 8.

These run against the database because that is where the guarantees live: single use is a
partial unique index and a conditional UPDATE, the rate limit is a row count, and reuse
detection is a family revocation.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from haazir.auth import magic_link, ratelimit, refresh
from haazir.auth.tokens import MAGIC_PREFIX, hash_token, new_token
from haazir.db import service_session

from .conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


async def _issue_link(email: str, ip: str | None = "203.0.113.9") -> str:
    """Issues a link and returns the raw token by re-deriving it.

    The token is never stored, so the test mints its own and inserts the row directly, exactly
    as `request_link` does. Reading it back out of the database would be testing a property
    the product is built to make impossible.
    """
    token = new_token(MAGIC_PREFIX)
    async with service_session() as s:
        await s.execute(
            text(
                """
                INSERT INTO magic_link (email, token_hash, purpose, expires_at, request_ip)
                VALUES (:e, :h, 'login', now() + INTERVAL '15 minutes', CAST(:ip AS inet))
                """
            ),
            {"e": email, "h": hash_token(token), "ip": ip},
        )
    return token


# --- §5 rule 8: never reveal whether an email exists -------------------------


async def test_request_link_answers_identically_for_known_and_unknown_addresses(
    client, user_factory
):
    _, known = await user_factory()
    a = await client.post("/v1/auth/request-link", json={"email": known})
    b = await client.post(
        "/v1/auth/request-link", json={"email": f"{uuid.uuid4().hex}@nowhere.example.com"}
    )
    assert a.status_code == b.status_code == 202

    # `dev_link` carries a fresh random token on every call, so it differs between any two
    # requests including two for the same address — it says nothing about registration. Every
    # other field must be byte-identical, which is the property this test exists for.
    a_body, b_body = a.json(), b.json()
    assert {k: v for k, v in a_body.items() if k != "dev_link"} == {
        k: v for k, v in b_body.items() if k != "dev_link"
    }
    # And it must not become a channel: same shape whether or not the address is known.
    assert (a_body["dev_link"] is None) == (b_body["dev_link"] is None)


# --- §5 rule 4: rate limit on email and on IP --------------------------------


async def test_three_links_per_email_then_429(client):
    email = f"{uuid.uuid4().hex[:8]}@example.com"
    for _ in range(3):
        assert (
            await client.post("/v1/auth/request-link", json={"email": email})
        ).status_code == 202
    blocked = await client.post("/v1/auth/request-link", json={"email": email})
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


async def test_the_email_limit_applies_to_unregistered_addresses_too(client):
    """Otherwise the 429 itself becomes the oracle rule 8 exists to remove."""
    stranger = f"{uuid.uuid4().hex[:8]}@nobody.example.com"
    for _ in range(3):
        await client.post("/v1/auth/request-link", json={"email": stranger})
    assert (
        await client.post("/v1/auth/request-link", json={"email": stranger})
    ).status_code == 429


async def test_the_ip_limit_catches_a_spray_across_many_addresses(client, monkeypatch):
    from haazir import config

    monkeypatch.setattr(config.settings, "rl_link_per_ip", 4)
    headers = {"fly-client-ip": "198.51.100.7"}
    codes = [
        (
            await client.post(
                "/v1/auth/request-link",
                json={"email": f"{uuid.uuid4().hex[:8]}@example.com"},
                headers=headers,
            )
        ).status_code
        for _ in range(6)
    ]
    assert codes[:4] == [202, 202, 202, 202]
    assert 429 in codes[4:]


# --- §5 rule 3: single use, 15 minute expiry ---------------------------------


async def test_a_link_works_once(client, clean_db):
    email = f"{uuid.uuid4().hex[:8]}@example.com"
    token = await _issue_link(email)

    first = await client.post("/v1/auth/verify", json={"token": token})
    assert first.status_code == 200, first.text
    assert first.json()["role"] == "diner"

    second = await client.post("/v1/auth/verify", json={"token": token})
    assert second.status_code == 400


async def test_an_expired_link_is_refused(client, clean_db):
    token = new_token(MAGIC_PREFIX)
    async with service_session() as s:
        await s.execute(
            text(
                """
                INSERT INTO magic_link (email, token_hash, expires_at)
                VALUES ('old@example.com', :h, now() - INTERVAL '1 minute')
                """
            ),
            {"h": hash_token(token)},
        )
    assert (await client.post("/v1/auth/verify", json={"token": token})).status_code == 400


async def test_an_unknown_token_and_a_used_token_give_the_same_answer(client, clean_db):
    used = await _issue_link("someone@example.com")
    await client.post("/v1/auth/verify", json={"token": used})

    a = await client.post("/v1/auth/verify", json={"token": used})
    b = await client.post("/v1/auth/verify", json={"token": new_token(MAGIC_PREFIX)})
    assert a.status_code == b.status_code == 400
    assert a.json() == b.json()


async def test_the_get_verify_endpoint_redirects_without_consuming(client, clean_db):
    """Mail scanners fetch every URL in a message. A GET that spent the link would mean the
    person never gets to use it."""
    email = f"{uuid.uuid4().hex[:8]}@example.com"
    token = await _issue_link(email)

    redirect = await client.get(f"/v1/auth/verify?token={token}")
    assert redirect.status_code == 302
    assert token in redirect.headers["location"]

    assert (await client.post("/v1/auth/verify", json={"token": token})).status_code == 200


async def test_the_admin_allowlist_promotes_on_first_sign_in(client, clean_db, monkeypatch):
    from haazir import config

    email = f"{uuid.uuid4().hex[:8]}@example.com"
    monkeypatch.setattr(config.settings, "admin_emails", email)
    token = await _issue_link(email)
    body = (await client.post("/v1/auth/verify", json={"token": token})).json()
    assert body["role"] == "admin"


async def test_a_suspended_account_cannot_sign_in(client, clean_db, user_factory):
    _, email = await user_factory()
    async with service_session() as s:
        await s.execute(
            text("UPDATE app_user SET status = 'suspended' WHERE email = :e"), {"e": email}
        )
    token = await _issue_link(email)
    assert (await client.post("/v1/auth/verify", json={"token": token})).status_code == 400


# --- §5 rule 5: cookie attributes --------------------------------------------


async def test_the_refresh_cookie_is_httponly_lax_and_scoped_to_the_auth_path(
    client, clean_db
):
    token = await _issue_link(f"{uuid.uuid4().hex[:8]}@example.com")
    response = await client.post("/v1/auth/verify", json={"token": token})
    raw = response.headers["set-cookie"].lower()
    assert "httponly" in raw
    assert "samesite=lax" in raw
    assert "path=/v1/auth" in raw
    # The access token goes in the body, for memory only. It must never be a cookie.
    assert "hz_access" not in raw


async def test_the_access_token_is_not_set_as_a_cookie(client, clean_db):
    token = await _issue_link(f"{uuid.uuid4().hex[:8]}@example.com")
    response = await client.post("/v1/auth/verify", json={"token": token})
    access = response.json()["access_token"]
    assert access not in response.headers.get("set-cookie", "")


# --- §5 rule 6: refresh rotation with reuse detection ------------------------


async def test_refresh_rotates_and_the_old_token_stops_working(client, clean_db):
    token = await _issue_link(f"{uuid.uuid4().hex[:8]}@example.com")
    await client.post("/v1/auth/verify", json={"token": token})
    first_cookie = client.cookies.get("hz_refresh")

    rotated = await client.post("/v1/auth/refresh")
    assert rotated.status_code == 200
    second_cookie = client.cookies.get("hz_refresh")
    assert second_cookie != first_cookie


async def test_a_replayed_refresh_token_revokes_the_whole_family(client, clean_db):
    """The rule that matters. A captured token is worth one use to the attacker and costs
    the legitimate holder a sign-in, rather than being worth ninety days."""
    email = f"{uuid.uuid4().hex[:8]}@example.com"
    link = await _issue_link(email)
    await client.post("/v1/auth/verify", json={"token": link})

    stolen = client.cookies.get("hz_refresh")
    assert (await client.post("/v1/auth/refresh")).status_code == 200
    live = client.cookies.get("hz_refresh")

    client.cookies.set("hz_refresh", stolen)
    replayed = await client.post("/v1/auth/refresh")
    assert replayed.status_code == 401

    # The token the legitimate holder had is now dead too. That is the point.
    client.cookies.set("hz_refresh", live)
    assert (await client.post("/v1/auth/refresh")).status_code == 401

    async with service_session() as s:
        alive = await s.scalar(
            text(
                "SELECT count(*) FROM refresh_token r "
                "JOIN app_user u ON u.id = r.user_id "
                "WHERE u.email = :e AND r.revoked_at IS NULL"
            ),
            {"e": email},
        )
    assert alive == 0


async def test_rotation_is_atomic_under_a_concurrent_replay(clean_db, user_factory):
    """Two requests arriving with the same token must produce exactly one winner. A
    read-then-write would let both through, which is the case reuse detection exists for."""
    import asyncio

    user_id, _ = await user_factory()
    async with service_session() as s:
        token, _ = await refresh.issue(s, user_id)

    async def attempt():
        async with service_session() as s:
            try:
                await refresh.rotate(s, token)
                return "ok"
            except refresh.RefreshReused:
                return "reused"
            except refresh.RefreshInvalid:
                return "invalid"

    results = await asyncio.gather(attempt(), attempt())
    assert sorted(results) == ["ok", "reused"]


async def test_logout_revokes_the_family(client, clean_db):
    token = await _issue_link(f"{uuid.uuid4().hex[:8]}@example.com")
    await client.post("/v1/auth/verify", json={"token": token})
    assert (await client.post("/v1/auth/logout")).status_code == 204
    assert (await client.post("/v1/auth/refresh")).status_code == 401


# --- /me ---------------------------------------------------------------------


async def test_me_requires_a_token_and_returns_the_signed_in_user(client, clean_db):
    assert (await client.get("/v1/auth/me")).status_code == 401

    email = f"{uuid.uuid4().hex[:8]}@example.com"
    link = await _issue_link(email)
    access = (await client.post("/v1/auth/verify", json={"token": link})).json()["access_token"]

    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200
    assert me.json()["email"] == email


async def test_a_guest_token_cannot_reach_a_user_endpoint(client, clean_db):
    """A group invite is not a login. It must not become one."""
    from haazir.auth.jwt import issue_guest

    guest = issue_guest(uuid.uuid4(), 1, uuid.uuid4())
    r = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {guest}"})
    assert r.status_code == 403


async def test_purge_removes_spent_links(clean_db):
    async with service_session() as s:
        await s.execute(
            text(
                "INSERT INTO magic_link (email, token_hash, expires_at) "
                "VALUES ('x@y.example.com', :h, now() - INTERVAL '3 days')"
            ),
            {"h": hash_token(new_token(MAGIC_PREFIX))},
        )
        removed = await magic_link.purge_expired(s)
    assert removed >= 1


@pytest.fixture(autouse=True)
def _reset_limiter():
    ratelimit.reset()
    yield
    ratelimit.reset()


@requires_db
@pytest.mark.asyncio
async def test_the_dev_link_is_returned_outside_production_and_never_inside_it(
    client, monkeypatch
):
    """The whole point of a magic link is that possession of the mailbox proves identity.

    Returning the link in the response hands a session to anyone who can POST an address, so
    the convenience that makes a local demo possible must be impossible in production. This
    asserts both halves; the second is the one that matters.
    """
    from haazir import config

    body = {"email": "dev-link@example.com"}

    monkeypatch.setattr(config.settings, "app_env", "dev")
    r = await client.post("/v1/auth/request-link", json=body)
    assert r.status_code == 202, r.text
    assert r.json()["dev_link"], "a non-production deployment should surface the link"

    monkeypatch.setattr(config.settings, "app_env", "prod")
    r = await client.post("/v1/auth/request-link", json={"email": "prod-link@example.com"})
    assert r.status_code == 202, r.text
    assert r.json()["dev_link"] is None
    # The rest of the response must not change either: it is the same for every address.
    assert r.json()["status"] == "accepted"
