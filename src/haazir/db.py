"""Async engine, session dependency, and the per-transaction RLS claim contract.

Two things in this module carry real weight.

**Pool settings.** Neon's free plan gives 100 compute-hours a month and suspends the compute
after five minutes idle. A pool that holds connections open means the compute never suspends
and the month's allowance is gone in about two weeks. `pool_recycle` sits under Neon's idle
timeout so sockets are dropped before Neon drops them for us.

**Claims.** Row-level security here is driven by `SET LOCAL app.*` settings rather than by the
database role, because the same connection serves an anonymous reader, a logged-in diner, a
staff tablet and a group guest within one second of each other. Every request transaction
writes the full claim set, including the empty values, never a subset. A claim left over from
a previous transaction on a pooled connection would be a privilege leak, so `apply_claims`
always writes all seven keys, in one round trip.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

Role = Literal["anon", "diner", "guest", "staff", "owner", "admin"]


class Base(DeclarativeBase):
    pass


# --- claims ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Claims:
    """What the database is told about the caller. Derived only from a verified token."""

    role: Role = "anon"
    user_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    slot: int | None = None
    venue_id: uuid.UUID | None = None

    def as_settings(self) -> dict[str, str]:
        return {
            "app.role": self.role,
            "app.user_id": str(self.user_id) if self.user_id else "",
            "app.group_id": str(self.group_id) if self.group_id else "",
            "app.slot": str(self.slot) if self.slot is not None else "",
            "app.venue_id": str(self.venue_id) if self.venue_id else "",
        }

    # `app.service` and `app.solver` are deliberately absent. A Claims object cannot express
    # them, so no amount of getting the request-parsing wrong can produce one that does.


ANON = Claims()


# All seven claims in one statement. `set_config` returns its value, so several of them sit
# happily in one select list, and the transaction pays a single round trip instead of seven.
# That is not a micro-optimisation: every request writes this, and the API is in Singapore
# next to the database precisely because these hops are the budget. Seven of them per request
# would spend the co-location saving before any real query ran.
#
# The keys are literals in the SQL and only the values are bound, which is what keeps this
# safe. `set_config(key, value, is_local => true)` accepts bind parameters; interpolating a
# user id into a `SET LOCAL` statement would put an injection hole in exactly the code that
# is meant to be the security boundary.
_APPLY_CLAIMS_SQL = text(
    "SELECT set_config('app.role',     :role,     true),"
    "       set_config('app.user_id',  :user_id,  true),"
    "       set_config('app.group_id', :group_id, true),"
    "       set_config('app.slot',     :slot,     true),"
    "       set_config('app.venue_id', :venue_id, true),"
    "       set_config('app.service',  :service,  true),"
    "       set_config('app.solver',   :solver,   true)"
)


async def apply_claims(
    session: AsyncSession, claims: Claims, *, service: bool = False, solver: bool = False
) -> None:
    """Write the caller's claims for the current transaction.

    `service` and `solver` are keyword-only and default to off. Nothing derived from a request
    ever passes them; only `service_session()` and `solver_session()` do.
    """
    settings_map = claims.as_settings()
    await session.execute(
        _APPLY_CLAIMS_SQL,
        {
            "role": settings_map["app.role"],
            "user_id": settings_map["app.user_id"],
            "group_id": settings_map["app.group_id"],
            "slot": settings_map["app.slot"],
            "venue_id": settings_map["app.venue_id"],
            "service": "on" if service else "",
            "solver": "on" if solver else "",
        },
    )


# --- engine ------------------------------------------------------------------


def _requires_ssl(url: str) -> bool:
    """Neon always needs TLS; a Postgres on localhost has no certificate to offer.

    Decided from the URL being connected to, not from `settings.database_url`. The two differ
    whenever migrations, tests or a rotation script point at a different branch or host than
    the app does, and an SSL decision borrowed from the wrong URL fails with an unhelpful
    handshake error on one side or the other.
    """
    if not url:
        return False
    host = urlsplit(url).hostname or ""
    return host not in {"localhost", "127.0.0.1", "::1", ""}


def _connect_args(url: str) -> dict[str, Any]:
    args: dict[str, Any] = {
        # asyncpg caches prepared statements per connection. PgBouncer in transaction mode,
        # which is what Neon's pooled endpoint runs, hands the next statement to a different
        # server connection and the cached plan is not there. Disabling the cache is the
        # documented fix; without it you get intermittent InvalidSQLStatementNameError under
        # concurrency and nowhere else.
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "server_settings": {"application_name": "haazir-api"},
    }
    if _requires_ssl(url):
        args["ssl"] = "require"
    return args


def make_engine(url: str | None = None, **overrides: Any):
    target = url or settings.sqlalchemy_url
    return create_async_engine(
        target,
        echo=settings.db_echo,
        pool_recycle=280,
        pool_timeout=10,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args=_connect_args(target),
        **overrides,
    )


engine = make_engine() if settings.sqlalchemy_url else None

SessionLocal: async_sessionmaker[AsyncSession] | None = (
    async_sessionmaker(engine, expire_on_commit=False, autoflush=False) if engine else None
)


def _require_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if SessionLocal is None:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy api/.env.example to api/.env and paste the Neon "
            "pooled connection string."
        )
    return SessionLocal


# --- session dependencies ----------------------------------------------------


async def get_session() -> AsyncIterator[AsyncSession]:
    """Plain session with anonymous claims. Routers that need a principal depend on
    `auth.deps.session_for_caller` instead, which applies the caller's claims."""
    async with _require_sessionmaker()() as session:
        await apply_claims(session, ANON)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@contextlib.asynccontextmanager
async def session_scope(claims: Claims = ANON) -> AsyncIterator[AsyncSession]:
    """For background jobs, ingestion and tests."""
    async with _require_sessionmaker()() as session:
        await apply_claims(session, claims)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@contextlib.asynccontextmanager
async def service_session() -> AsyncIterator[AsyncSession]:
    """Ingestion, the estimator and the background jobs.

    Every RLS table is FORCE ROW LEVEL SECURITY, which applies to the table owner too, so the
    service paths need a way through that a request cannot reach. `app.service` is that way:
    it is written as an empty string by every request transaction and set to 'on' only here.

    `group_constraint` has no service policy. Reading it needs `solver_session()`.
    """
    async with _require_sessionmaker()() as session:
        await apply_claims(session, ANON, service=True)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@contextlib.asynccontextmanager
async def solver_session(group_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """The only path in the codebase that can read `group_constraint` rows in bulk.

    `group_constraint` has FORCE ROW LEVEL SECURITY and no policy granting the group creator,
    or anybody else, read access to another member's row. The solver needs every row, so it
    opens a transaction carrying `app.solver = 'on'`, which exactly one policy accepts.

    This is safe only because of an invariant that must hold: nothing derived from a request
    may ever set `app.solver`. `Claims` cannot express it and `apply_claims` writes it as an
    empty string unless asked, so a value cannot survive on a pooled connection into the next
    caller's transaction. Callers here must return the satisfaction vector, never the rows.
    """
    async with _require_sessionmaker()() as session:
        await apply_claims(session, Claims(group_id=group_id), solver=True)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def ping() -> dict[str, Any]:
    """Health probe. Confirms the connection and that the extensions the product depends on
    are actually installed, because a Neon branch created without them fails much later and
    much less obviously."""
    sql = text(
        "SELECT current_database() AS db, "
        "(SELECT extversion FROM pg_extension WHERE extname = 'postgis') AS postgis, "
        "(SELECT extversion FROM pg_extension WHERE extname = 'vector') AS vector, "
        "(SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm') AS pg_trgm, "
        "(SELECT extversion FROM pg_extension WHERE extname = 'citext') AS citext"
    )
    async with _require_sessionmaker()() as session:
        row = (await session.execute(sql)).mappings().one()
        return dict(row)


async def dispose() -> None:
    if engine is not None:
        await engine.dispose()
