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
from ..services import recompute

log = logging.getLogger("haazir.scheduler")

# Karachi, because "nightly" means the small hours there, not in UTC.
TZ = "Asia/Karachi"


async def _refresh_live_state() -> None:
    async with service_session() as session:
        written = await recompute.refresh_live_state(session)
    if written:
        log.info("refreshed live_state for %d venues", written)


async def _decay_facts() -> None:
    async with service_session() as session:
        rows = await recompute.decay_facts(session)
    log.info("decayed access facts on %d venues", rows)


async def _recompute_trust() -> None:
    async with service_session() as session:
        n = await recompute.recompute_trust(session)
    log.info("recomputed trust for %d venues", n)


async def _purge() -> None:
    """Expired magic links and spent group invites. §11."""
    from ..auth import group as group_auth
    from ..auth import magic_link

    async with service_session() as session:
        links = await magic_link.purge_expired(session)
        invites = await group_auth.purge_consumed(session)
    log.info("purged %d magic links and %d group invites", links, invites)


async def _create_partitions() -> None:
    async with service_session() as session:
        name = await recompute.create_next_partition(session)
    log.info("observation partition ready: %s", name)


def build() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TZ)

    # The loop that makes the product live. Sixty seconds is the plan's cadence and it is
    # also about as slow as it can be: a diner deciding where to eat will not wait longer
    # than that for an estimate to catch up with the room.
    scheduler.add_job(
        _refresh_live_state, IntervalTrigger(seconds=60),
        id="refresh_live_state", max_instances=1, coalesce=True,
        misfire_grace_time=30,
        # coalesce and max_instances together: if a cycle overruns, the next one is skipped
        # rather than queued. Stacking refreshes behind a slow one turns a hiccup into a
        # backlog that never clears.
    )

    scheduler.add_job(_decay_facts, CronTrigger(hour=3, minute=10), id="decay_facts")
    scheduler.add_job(_recompute_trust, CronTrigger(hour=3, minute=30), id="recompute_trust")
    scheduler.add_job(_purge, CronTrigger(hour=4, minute=0), id="purge")
    scheduler.add_job(
        _create_partitions, CronTrigger(day=1, hour=2, minute=0), id="create_partitions"
    )
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
