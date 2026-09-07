"""Rate limiting. Plan §5 rule 4.

Two different mechanisms, because two different threats.

**Magic links are limited from the database.** Counting rows in `magic_link` survives a
restart and a deploy. An in-memory counter does not, and the attack it stops is real: without
a per-email limit, anyone can type a stranger's address into the form repeatedly and this
service becomes the thing sending them fifty emails. The limit is on both the email and the
IP, because either one alone is trivially sidestepped.

**Everything else is limited in process.** Staff state posts, check-ins and refreshes are
abuse control rather than a path to harming a third party, and losing the counter on a deploy
costs nothing. When the API runs on more than one instance this moves to Upstash; the
interface below is the same either way.
"""

from __future__ import annotations

import datetime as dt
import time
from collections import defaultdict, deque

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings


class RateLimited(Exception):
    def __init__(self, retry_after: int = 60) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


# --- in-process sliding window -----------------------------------------------

_WINDOWS: dict[str, deque[float]] = defaultdict(deque)


def check(key: str, limit: int, window_s: int) -> None:
    now = time.monotonic()
    bucket = _WINDOWS[key]
    cutoff = now - window_s
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= limit:
        raise RateLimited(retry_after=int(window_s - (now - bucket[0])) + 1)
    bucket.append(now)


def reset() -> None:
    """Tests only."""
    _WINDOWS.clear()


# --- database-backed, for magic links ----------------------------------------


async def check_magic_link(session: AsyncSession, email: str, ip: str | None) -> None:
    """Raise `RateLimited` if this email or this IP has asked for too many links.

    Counts issued rows rather than successful sends, so a send that fails still consumes
    quota. That is the safe direction: a retry storm against a broken mail provider must not
    become an unbounded loop of outbound mail once it recovers.
    """
    now = dt.datetime.now(dt.UTC)

    email_since = now - dt.timedelta(seconds=settings.rl_link_per_email_window)
    n_email = await session.scalar(
        text("SELECT count(*) FROM magic_link WHERE email = :e AND created_at > :since"),
        {"e": email, "since": email_since},
    )
    if (n_email or 0) >= settings.rl_link_per_email:
        raise RateLimited(retry_after=settings.rl_link_per_email_window)

    if ip:
        ip_since = now - dt.timedelta(seconds=settings.rl_link_per_ip_window)
        n_ip = await session.scalar(
            text(
                "SELECT count(*) FROM magic_link "
                "WHERE request_ip = CAST(:ip AS inet) AND created_at > :since"
            ),
            {"ip": ip, "since": ip_since},
        )
        if (n_ip or 0) >= settings.rl_link_per_ip:
            raise RateLimited(retry_after=settings.rl_link_per_ip_window)
