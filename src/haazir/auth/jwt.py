"""Access-token minting and verification.

Access tokens are short-lived JWTs. Refresh and device credentials are opaque strings backed
by a database row, because those need revocation and a JWT cannot be revoked: the whole point
of a signed token is that it verifies without a lookup, which is exactly the property you do
not want when a tablet is stolen.

The `role` claim is validated against a closed set on the way in and on the way out. Neither
`service` nor `solver` is a member, which is what makes the promise in `db.py` hold — no
token, however it was obtained or forged, can produce those flags.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import jwt
from jwt import InvalidTokenError

from ..config import settings
from ..db import Claims, Role

ALGORITHM = "HS256"
TokenType = Literal["access", "guest", "staff"]

# The complete set of roles a signed token may assert. Not `service`, not `solver`.
ISSUABLE_ROLES: frozenset[str] = frozenset({"diner", "owner", "admin", "guest", "staff"})


class AuthError(Exception):
    """Token was absent, malformed, expired or not trustworthy. Always a 401 to the caller,
    never a description of which of those it was."""


@dataclass(frozen=True, slots=True)
class Principal:
    """A verified caller. The only thing permitted to become `Claims`."""

    role: Role
    subject: uuid.UUID
    token_type: TokenType
    venue_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    slot: int | None = None
    email: str | None = None
    jti: str | None = None

    def to_claims(self) -> Claims:
        return Claims(
            role=self.role,
            user_id=self.subject if self.role in {"diner", "owner", "admin"} else None,
            group_id=self.group_id,
            slot=self.slot,
            venue_id=self.venue_id,
        )


def _require_secret() -> str:
    if not settings.jwt_secret or len(settings.jwt_secret) < 32:
        raise RuntimeError(
            "JWT_SECRET is missing or shorter than 32 characters. Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    return settings.jwt_secret


def _encode(payload: dict[str, Any], ttl: int) -> str:
    now = dt.datetime.now(dt.UTC)
    payload = {
        **payload,
        "iss": settings.jwt_issuer,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(seconds=ttl)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, _require_secret(), algorithm=ALGORITHM)


def issue_access(user_id: uuid.UUID, role: Role, email: str | None = None) -> str:
    if role not in {"diner", "owner", "admin"}:
        raise ValueError(f"{role!r} is not a user role")
    ttl = settings.jwt_admin_ttl if role == "admin" else settings.jwt_access_ttl
    return _encode({"sub": str(user_id), "role": role, "typ": "access", "email": email}, ttl)


def issue_guest(group_id: uuid.UUID, slot: int, member_id: uuid.UUID) -> str:
    """A group invite. Scoped to one group and one slot, and to nothing else in the product."""
    return _encode(
        {
            "sub": str(member_id),
            "role": "guest",
            "typ": "guest",
            "gid": str(group_id),
            "slot": int(slot),
        },
        settings.jwt_guest_ttl,
    )


def decode(token: str) -> Principal:
    try:
        payload = jwt.decode(
            token,
            _require_secret(),
            algorithms=[ALGORITHM],
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "iss", "sub", "role"]},
        )
    except InvalidTokenError as exc:
        raise AuthError(str(exc)) from exc

    role = payload.get("role")
    if role not in ISSUABLE_ROLES:
        raise AuthError("unknown role")

    typ = payload.get("typ")
    if typ not in {"access", "guest", "staff"}:
        raise AuthError("unknown token type")

    try:
        subject = uuid.UUID(payload["sub"])
        group_id = uuid.UUID(payload["gid"]) if payload.get("gid") else None
        venue_id = uuid.UUID(payload["vid"]) if payload.get("vid") else None
    except (ValueError, TypeError) as exc:
        raise AuthError("malformed subject") from exc

    if role == "guest" and (group_id is None or payload.get("slot") is None):
        raise AuthError("guest token is not scoped")

    slot = payload.get("slot")
    return Principal(
        role=role,
        subject=subject,
        token_type=typ,
        venue_id=venue_id,
        group_id=group_id,
        slot=int(slot) if slot is not None else None,
        email=payload.get("email"),
        jti=payload.get("jti"),
    )
