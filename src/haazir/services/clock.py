"""What time it is where the restaurant is.

`occupancy_prior` is indexed by `hour_of_week`, built during ingestion as
`weekday * 24 + hour` with weekday 0 = Sunday, matching Postgres `EXTRACT(DOW)` and the
`dayFactor` array in `app/assets/js/data.js`.

Reading it back needs the **venue's** local hour, not the server's. Karachi is UTC+5, so
computing this from `datetime.now(UTC)` asks the database for the three-in-the-afternoon row
at eight in the evening: a number that is not obviously wrong, on a screen whose entire claim
is that it knows what is true right now. It is also invisible in tests unless one is written
for it, which is why there is one.

The SQL form is the one to prefer. It reads `city.timezone`, so a second city works without
anybody remembering to pass a different string, and it needs no round trip of its own.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Karachi"

# `EXTRACT(DOW)` is 0 = Sunday, which is the convention the ingest wrote, so no shifting is
# needed here. Inline this into a query that already joins `city c`.
HOUR_OF_WEEK_SQL = (
    "(EXTRACT(DOW FROM now() AT TIME ZONE c.timezone)::int * 24"
    " + EXTRACT(HOUR FROM now() AT TIME ZONE c.timezone)::int)"
)


def hour_of_week(now: dt.datetime | None = None, tz: str = DEFAULT_TZ) -> int:
    """Python fallback, for code paths with no `city` row to hand.

    `datetime.weekday()` is 0 = Monday; the stored index is 0 = Sunday, hence the rotation.
    """
    local = (now or dt.datetime.now(dt.UTC)).astimezone(ZoneInfo(tz))
    return ((local.weekday() + 1) % 7) * 24 + local.hour


def local_now(tz: str = DEFAULT_TZ) -> dt.datetime:
    return dt.datetime.now(ZoneInfo(tz))
