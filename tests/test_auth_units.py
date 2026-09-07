"""The parts of §5 that can be proved without a database.

Rules 1, 2 and 7 are properties of token generation and comparison. Rule 5's cookie
attributes and rules 3, 4, 6 and 8 need the database and live in the integration tests.
"""

from __future__ import annotations

import time
import uuid

import pytest

from haazir.auth import ratelimit
from haazir.auth.jwt import AuthError, Principal, decode, issue_access, issue_guest
from haazir.auth.tokens import (
    DEVICE_PREFIX,
    MAGIC_PREFIX,
    REFRESH_PREFIX,
    hash_token,
    new_token,
    tokens_match,
)
from haazir.config import _to_asyncpg, to_psycopg_url

# --- §5 rule 1: entropy ------------------------------------------------------


def test_tokens_carry_256_bits_and_are_unique():
    tokens = {new_token(MAGIC_PREFIX) for _ in range(2000)}
    assert len(tokens) == 2000
    body = next(iter(tokens)).removeprefix(MAGIC_PREFIX)
    # token_urlsafe(32) base64url-encodes 32 bytes: 43 characters, no padding.
    assert len(body) == 43


def test_prefixes_are_distinct_so_a_bearer_can_be_routed_without_guessing():
    assert len({MAGIC_PREFIX, REFRESH_PREFIX, DEVICE_PREFIX}) == 3
    assert new_token(DEVICE_PREFIX).startswith(DEVICE_PREFIX)


# --- §5 rules 2 and 7: storage and comparison --------------------------------


def test_hash_is_stable_and_not_the_token():
    token = new_token(MAGIC_PREFIX)
    digest = hash_token(token)
    assert digest == hash_token(token)
    assert token not in digest
    assert len(digest) == 64


def test_tokens_match_is_true_only_for_the_original():
    token = new_token(REFRESH_PREFIX)
    digest = hash_token(token)
    assert tokens_match(token, digest)
    assert not tokens_match(new_token(REFRESH_PREFIX), digest)
    assert not tokens_match(token[:-1], digest)


# --- JWT ---------------------------------------------------------------------


def test_access_token_round_trips():
    uid = uuid.uuid4()
    principal = decode(issue_access(uid, "diner", "a@b.example.com"))
    assert principal == Principal(
        role="diner",
        subject=uid,
        token_type="access",
        email="a@b.example.com",
        jti=principal.jti,
    )


def test_guest_token_is_scoped_to_one_group_and_slot():
    gid, mid = uuid.uuid4(), uuid.uuid4()
    principal = decode(issue_guest(gid, 3, mid))
    assert principal.role == "guest"
    assert principal.group_id == gid
    assert principal.slot == 3
    # A guest is not a user. It must never populate app.user_id.
    assert principal.to_claims().user_id is None
    assert principal.to_claims().group_id == gid


def test_service_and_solver_are_not_issuable_roles():
    """The promise in db.py: no signed token can produce the flags that bypass RLS."""
    for role in ("service", "solver", "anon"):
        with pytest.raises(ValueError):
            issue_access(uuid.uuid4(), role)  # type: ignore[arg-type]


def test_a_tampered_token_is_refused():
    token = issue_access(uuid.uuid4(), "diner")
    head, payload, sig = token.split(".")
    with pytest.raises(AuthError):
        decode(f"{head}.{payload}.{sig[:-3]}abc")


def test_a_token_signed_with_another_key_is_refused():
    import jwt as pyjwt

    forged = pyjwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "role": "admin",
            "typ": "access",
            "iss": "haazir",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        },
        "not-the-real-secret",
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        decode(forged)


def test_an_unsigned_alg_none_token_is_refused():
    """`alg: none` is the oldest JWT attack there is. PyJWT refuses it when algorithms is an
    explicit list, and this test is what keeps that list explicit."""
    import base64
    import json

    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    forged = (
        b64({"alg": "none", "typ": "JWT"})
        + "."
        + b64(
            {
                "sub": str(uuid.uuid4()),
                "role": "admin",
                "typ": "access",
                "iss": "haazir",
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
            }
        )
        + "."
    )
    with pytest.raises(AuthError):
        decode(forged)


def test_an_expired_token_is_refused(monkeypatch):
    from haazir import config

    monkeypatch.setattr(config.settings, "jwt_access_ttl", -10)
    with pytest.raises(AuthError):
        decode(issue_access(uuid.uuid4(), "diner"))


# --- §5 rule 4: the in-process limiter ---------------------------------------


def test_sliding_window_allows_the_limit_and_refuses_the_next():
    ratelimit.reset()
    for _ in range(3):
        ratelimit.check("k", limit=3, window_s=60)
    with pytest.raises(ratelimit.RateLimited) as exc:
        ratelimit.check("k", limit=3, window_s=60)
    assert exc.value.retry_after > 0


def test_windows_are_per_key():
    ratelimit.reset()
    ratelimit.check("a", limit=1, window_s=60)
    ratelimit.check("b", limit=1, window_s=60)  # must not raise


# --- Neon URL handling -------------------------------------------------------


def test_libpq_only_parameters_are_stripped_for_asyncpg():
    """asyncpg raises `TypeError: connect() got an unexpected keyword argument 'sslmode'` on
    a Neon URL pasted verbatim, and the traceback looks like bad credentials."""
    neon = (
        "postgresql://u:p@ep-x-pooler.ap-southeast-1.aws.neon.tech/haazir"
        "?sslmode=require&channel_binding=require"
    )
    converted = _to_asyncpg(neon)
    assert converted.startswith("postgresql+asyncpg://")
    assert "sslmode" not in converted
    assert "channel_binding" not in converted


def test_other_query_parameters_survive():
    converted = _to_asyncpg("postgres://u:p@h/db?sslmode=require&application_name=x")
    assert "application_name=x" in converted


def test_postgres_scheme_is_normalised():
    assert _to_asyncpg("postgres://u:p@h/db").startswith("postgresql+asyncpg://")
    assert to_psycopg_url("postgresql+asyncpg://u:p@h/db").startswith("postgresql://")


def test_empty_url_stays_empty():
    assert _to_asyncpg("") == ""


def test_a_cross_site_cookie_must_be_secure_or_the_browser_discards_it():
    """`SameSite=None` without `Secure` is dropped by every current browser.

    Worth a test because the failure is invisible from the server: the cookie is set, the
    response is 200, and the session simply does not survive the access token — with nothing
    in any log to say why. The combination that produces it is a two-line config mistake, and
    deployed apart from the web app it is the *default* mistake to make.
    """
    from haazir.config import Settings

    with pytest.raises(ValueError, match="COOKIE_SECURE"):
        Settings(
            jwt_secret="x" * 40,
            cookie_samesite="none",
            cookie_secure=False,
        )

    # And the combination that works is accepted.
    ok = Settings(jwt_secret="x" * 40, cookie_samesite="none", cookie_secure=True)
    assert ok.cookie_samesite == "none"


def test_samesite_rejects_a_value_a_browser_would_not_understand():
    from haazir.config import Settings

    with pytest.raises(ValueError):
        Settings(jwt_secret="x" * 40, cookie_samesite="sometimes")
