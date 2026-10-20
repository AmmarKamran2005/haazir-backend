"""The database may sleep: no timer work while idle, and the cache answers without a session."""

from __future__ import annotations

import time

from haazir.services import activity, respcache
from haazir.workers import scheduler


async def test_the_refresh_timer_does_nothing_while_idle(monkeypatch):
    called = []

    async def fake_refresh():
        called.append(1)

    monkeypatch.setattr(scheduler, "refresh_once", fake_refresh)
    monkeypatch.setattr(activity, "_last", time.monotonic() - activity.IDLE_AFTER_S - 5)
    await scheduler._refresh_live_state()
    assert called == []
    assert activity.touch() is True  # the first request after idle wakes it
    await scheduler._refresh_live_state()
    assert called == [1]


def test_health_probes_are_not_activity():
    assert activity.ignored("/health") and activity.ignored("/health/db")
    assert not activity.ignored("/v1/venues/x")


def test_only_public_reads_are_cached():
    assert respcache.ttl_for("/v1/venues/naseeb") == 30
    assert respcache.ttl_for("/v1/deals") == 60
    assert respcache.ttl_for("/v1/venues/naseeb/live/stream") == 0
    assert respcache.ttl_for("/v1/venues/naseeb/tables") == 0
    assert respcache.ttl_for("/v1/owner/venues") == 0
