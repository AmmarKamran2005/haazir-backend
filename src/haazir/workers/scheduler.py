"""Background jobs. Plan §11.

APScheduler inside the API process, which is sufficient at this scale and has one property a
separate worker does not: the SSE hub lives in this process, so a job that recomputes a
venue's state can publish it to subscribers without a message broker in between.

That also fixes the limit of the last phase. `refresh_live_state` existed and worked, but only
ran when a staff tap called it, so a venue nobody touched kept whatever estimate it last had
even as the evening moved past it. The prior is a function of the clock; something has to
re-read it.

**Only one instance may run these.** Two processes both writing `live_state` every minute
would not corrupt anything, but they would double the compute bill on a plan with a hundred
hours a month. `RUN_SCHEDULER` gates it, and the deployment runs a single machine (§8).
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..config import settings
from ..db import service_session
from ..services import activity, recompute

log = logging.getLogger("haazir.scheduler")

# Karachi, because "nightly" means the small hours there, not in UTC.
TZ = "Asia/Karachi"


async def refresh_once() -> None:
    """One refresh, unconditionally. Also run by the first request after an idle spell."""
    try:
        async with service_session() as session:
            written = await recompute.refresh_live_state(session)
    except Exception as exc:  # noqa: BLE001 — a background job logs, it does not raise
        log.warning("live_state refresh failed: %s", exc)
        return
    if written:
        log.info("refreshed live_state for %d venues", written)


async def _refresh_live_state() -> None:
    """Every few minutes, and only while somebody is using the API.

    Idle, it does no database I/O at all, so Neon can suspend its compute. Readings from
    staff, cameras and check-ins re-fuse their own venue the moment they arrive; this timer
    only moves the hour-of-week prior along for everybody else, and nobody needs that while
    nobody is looking.
    """
    if activity.idle():
        return
    await refresh_once()


async def _nightly() -> None:
    """Every nightly job in one wake of the database, one after another."""
    from ..auth import group as group_auth
    from ..auth import magic_link

    steps = [
        ("decay_facts", recompute.decay_facts),
        ("learn_priors", recompute.learn_priors),
        ("recompute_trust", recompute.recompute_trust),
        ("purge_links", magic_link.purge_expired),
        ("purge_invites", group_auth.purge_consumed),
        ("partitions", recompute.create_next_partition),
    ]
    for name, job in steps:
        try:
            async with service_session() as session:
                result = await job(session)
            log.info("nightly %s: %s", name, result)
        except Exception as exc:  # noqa: BLE001 — one failed step must not skip the rest
            log.warning("nightly %s failed: %s", name, exc)


def build() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TZ)

    # The loop that makes the product live. Sixty seconds is the plan's cadence and it is
    # also about as slow as it can be: a diner deciding where to eat will not wait longer
    # than that for an estimate to catch up with the room.
    # While the API is in use, the prior moves with the clock every two minutes; idle, the job
    # returns before touching the database (see `_refresh_live_state`).
    scheduler.add_job(
        _refresh_live_state, IntervalTrigger(seconds=120),
        id="refresh_live_state", max_instances=1, coalesce=True,
        misfire_grace_time=30,
        # coalesce and max_instances together: if a cycle overruns, the next one is skipped
        # rather than queued. Stacking refreshes behind a slow one turns a hiccup into a
        # backlog that never clears.
    )
    scheduler.add_job(_nightly, CronTrigger(hour=3, minute=10), id="nightly",
                      max_instances=1, coalesce=True)
    return scheduler


_scheduler: AsyncIOScheduler | None = None


def start() -> AsyncIOScheduler | None:
    """Called from the app lifespan. Returns None when the scheduler is switched off."""
    global _scheduler
    if not settings.run_scheduler:
        log.info("scheduler disabled (RUN_SCHEDULER=false)")
        return None
    if _scheduler is not None:
        return _scheduler
    _scheduler = build()
    _scheduler.start()
    log.info("scheduler started: %s", ", ".join(j.id for j in _scheduler.get_jobs()))
    return _scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
