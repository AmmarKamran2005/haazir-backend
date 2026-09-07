"""Health and readiness.

`/health` is the liveness probe: it answers without touching the database, so a database
blip does not make the platform kill and restart a process that is working fine.

`/health/db` is the readiness probe and Phase 1's acceptance check. It fails when an
extension the product depends on is missing, rather than reporting green and letting the
first geo query be the thing that discovers it.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from .. import db
from ..config import settings

router = APIRouter(tags=["health"])

REQUIRED_EXTENSIONS = ("postgis", "vector", "pg_trgm", "citext")


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "env": settings.app_env, "version": "0.1.0"}


@router.get("/health/db")
async def health_db(response: Response) -> dict:
    if db.engine is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unconfigured", "detail": "DATABASE_URL is not set"}

    try:
        info = await db.ping()
    except Exception as exc:  # noqa: BLE001 — the probe reports the failure, it does not raise
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "down", "detail": type(exc).__name__, "message": str(exc)[:300]}

    missing = [e for e in REQUIRED_EXTENSIONS if not info.get(e)]
    if missing:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "missing_extensions": missing, "extensions": info}

    return {"status": "ok", "database": info["db"], "extensions": info}
