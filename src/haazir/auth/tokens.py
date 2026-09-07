"""Secret generation and hashing. Plan §5 rules 1, 2, 7.

Every credential in this system is created here and nowhere else, so the three rules that
matter are in one place instead of repeated across four modules.

**Entropy.** `secrets.token_urlsafe(32)` gives 256 bits from the OS CSPRNG. `uuid4` gives 122
bits from a generator with no such guarantee, and a magic-link token is a bearer credential:
guessing one is logging in as that person.

**Storage.** The database stores `sha256(token)`. A dump must not be a set of working
credentials. SHA-256 rather than a password hash is right here and would be wrong for a
password: these are 256-bit random strings, so there is no dictionary to run and no reason to
pay Argon2's cost on the hot path.

**Prefixes.** Each token kind carries one. It lets `deps.py` route a bearer credential without
guessing, and it lets a secret scanner recognise a leaked HAAZIR token in a log or a repo.
"""

from __future__ import annotations

import hashlib
import secrets

MAGIC_PREFIX = "hzm_"
REFRESH_PREFIX = "hzr_"
DEVICE_PREFIX = "hzd_"

_ENTROPY_BYTES = 32


def new_token(prefix: str) -> str:
    return f"{prefix}{secrets.token_urlsafe(_ENTROPY_BYTES)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(presented: str, stored_hash: str) -> bool:
    """Constant-time comparison (§5 rule 7).

    Lookups are by hash on a unique index, so the database has already done an equality test
    by the time this is called. It runs anyway: it costs nothing, it keeps the rule visible at
    the point it applies, and it stays correct if a lookup path ever changes to fetch by id.
    """
    return secrets.compare_digest(hash_token(presented), stored_hash)


def hash_email(email: str) -> str:
    """For rate-limit keys and logs. The address itself never needs to appear in either."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:16]


def author_hash(name: str) -> str:
    """`review_sample.author_hash`. Enough to notice one author flooding one venue, not
    enough to identify them."""
    return hashlib.sha256(name.strip().lower().encode("utf-8")).hexdigest()
