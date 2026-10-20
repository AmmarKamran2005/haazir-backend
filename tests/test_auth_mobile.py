"""Mobile sign-in against the database: code, body refresh, demo accounts."""

from __future__ import annotations

import pytest

from haazir.config import settings

from .conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


async def test_a_code_signs_in_once_and_the_refresh_token_rotates_in_the_body(client):
    r = await client.post("/v1/auth/request-code", json={"email": "phone@example.com"})
    assert r.status_code == 200, r.text
    code = r.json()["dev_code"]
    assert len(code) == 6

    r = await client.post("/v1/auth/verify-code", json={"email": "phone@example.com",
                                                        "code": code})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["refresh_token"].startswith("hzr_")
    assert body["user"]["email"] == "phone@example.com" and body["user"]["role"] == "diner"

    again = await client.post("/v1/auth/verify-code", json={"email": "phone@example.com",
                                                            "code": code})
    assert again.status_code == 400

    r = await client.post("/v1/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 200, r.text
    assert r.json()["refresh_token"] not in (None, body["refresh_token"])
    assert "hz_refresh" not in r.cookies


async def test_five_wrong_codes_spend_the_code(client):
    code = (await client.post("/v1/auth/request-code",
                              json={"email": "guess@example.com"})).json()["dev_code"]
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    for _ in range(5):
        r = await client.post("/v1/auth/verify-code",
                              json={"email": "guess@example.com", "code": wrong})
        assert r.status_code == 400
    r = await client.post("/v1/auth/verify-code", json={"email": "guess@example.com",
                                                        "code": code})
    assert r.status_code == 400


async def test_demo_owner_owns_the_hero_venue(client, venue_factory, monkeypatch):
    from sqlalchemy import text

    from haazir.db import service_session

    venue_id = await venue_factory("Hero")
    async with service_session() as s:
        slug = await s.scalar(text("SELECT slug FROM venue WHERE id = :v"), {"v": venue_id})

    assert (await client.post("/v1/auth/demo", json={"role": "owner"})).status_code == 404

    monkeypatch.setattr(settings, "demo_login", True)
    monkeypatch.setattr(settings, "demo_hero_venue", slug)
    r = await client.post("/v1/auth/demo", json={"role": "owner"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    mine = (await client.get("/v1/owner/venues",
                             headers={"Authorization": f"Bearer {token}"})).json()
    assert [v["slug"] for v in mine] == [slug]
