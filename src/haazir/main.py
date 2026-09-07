"""FastAPI application: lifespan, middleware, routers.

`/health` is deliberately more than a 200. It reports whether `postgis`, `vector`, `pg_trgm`
and `citext` are actually present, because a Neon branch created without them fails much
later and much less obviously: `alembic upgrade head` succeeds, the API starts, and the first
geo query is the thing that breaks.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from . import db
from .auth.throttle import ThrottleMiddleware
from .config import settings
from .routers import (
    admin,
    ask,
    auth,
    city,
    diner,
    dishes,
    group,
    health,
    ingest,
    live,
    owner,
    search,
    staff,
    venues,
)
from .workers import scheduler

log = logging.getLogger("haazir")

REQUIRED_EXTENSIONS = ("postgis", "vector", "pg_trgm", "citext")


def _configure_sentry() -> None:
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        log.warning("SENTRY_DSN is set but sentry-sdk is not installed; skipping")
        return
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        traces_sample_rate=0.1 if settings.is_prod else 1.0,
        send_default_pii=False,  # observations are counts, not identities
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s  %(message)s",
    )
    _configure_sentry()

    if db.engine is None:
        log.warning("DATABASE_URL is not set. The API is up but every data route will fail.")
    else:
        try:
            info = await db.ping()
            missing = [e for e in REQUIRED_EXTENSIONS if not info.get(e)]
            if missing:
                # Loud, but not fatal: a developer running a partial local database should
                # still get a server they can poke at.
                log.error("database is missing required extensions: %s", ", ".join(missing))
            else:
                log.info("database ok: %s, postgis %s, vector %s", info["db"],
                         info["postgis"], info["vector"])
        except Exception as exc:  # noqa: BLE001 — startup must report, not crash
            log.error("database unreachable at startup: %s", exc)

    if db.engine is not None:
        scheduler.start()

    yield

    scheduler.shutdown()
    await db.dispose()


app = FastAPI(
    title="HAAZIR API",
    version="0.1.0",
    description="The live truth layer for eating out. Karachi.",
    docs_url=None if settings.is_prod else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_prod else "/openapi.json",
    lifespan=lifespan,
)

# Outermost of the two, so a throttled request is rejected before CORS work is done and
# before any handler is reached. Reads pass straight through; see the module note.
app.add_middleware(ThrottleMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=True,  # the refresh cookie needs this
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
    max_age=600,
)


@app.middleware("http")
async def timing_header(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Response-Time-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
    return response


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(staff.router)
app.include_router(venues.router)
app.include_router(dishes.router)
app.include_router(city.router)
app.include_router(ingest.router)
app.include_router(search.router)
app.include_router(live.router)
app.include_router(diner.router)
app.include_router(group.router)
app.include_router(ask.router)
app.include_router(owner.router)
