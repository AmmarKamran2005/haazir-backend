"""Google Sign-In: verify an ID token and return the identity it asserts.

Verified by Google's `tokeninfo` endpoint rather than by fetching JWKS and checking the
signature locally. One HTTPS call per sign-in is nothing at this scale, it needs no new
dependency, and Google does the signature, expiry and issuer checks itself. What stays our
job is the audience: a token minted for somebody else's app is a perfectly valid Google token
and must still be refused, which is what `GOOGLE_CLIENT_IDS` is for.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..config import settings

TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
_TIMEOUT = httpx.Timeout(6.0, connect=3.0)


class GoogleInvalid(Exception):
    """Not a usable Google identity. Always a 401, never a reason."""


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    sub: str
    email: str
    name: str | None
    picture: str | None


def check_claims(claims: dict, audiences: list[str]) -> GoogleIdentity:
    """The checks that are ours rather than Google's. Pure, so it is testable offline."""
    if not audiences:
        raise GoogleInvalid("GOOGLE_CLIENT_IDS is not configured")
    if claims.get("aud") not in audiences:
        raise GoogleInvalid("audience")
    if claims.get("iss") not in ISSUERS:
        raise GoogleInvalid("issuer")
    if str(claims.get("email_verified")).lower() != "true" or not claims.get("email"):
        raise GoogleInvalid("email not verified")
    return GoogleIdentity(
        sub=str(claims["sub"]),
        email=str(claims["email"]).strip().lower(),
        name=claims.get("name"),
        picture=claims.get("picture"),
    )


async def verify(id_token: str) -> GoogleIdentity:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(TOKENINFO, params={"id_token": id_token})
    except httpx.HTTPError as exc:
        raise GoogleInvalid("google unreachable") from exc
    if r.status_code != 200:
        raise GoogleInvalid("rejected by google")
    return check_claims(r.json(), settings.google_client_id_list)
