"""In-process pub/sub for the live feed. Plan §8.

One long-running FastAPI instance needs no database pub/sub, so this is a
`dict[venue_id, set[Queue]]` and about eighty lines. Postgres `LISTEN/NOTIFY` would not work
here anyway: it needs a session-scoped connection, and Neon's pooled endpoint is PgBouncer in
transaction mode, which hands the next statement to a different backend.

When the API runs on more than one instance, `publish` becomes an Upstash Redis publish and
`subscribe` a Redis subscription. Nothing else in the codebase changes, which is why every
caller goes through this module rather than touching the dictionary.

**Backlog and Last-Event-ID.** Each venue keeps its last few events with monotonic ids. A
client whose connection dropped reconnects with `Last-Event-ID` and gets only what it missed.
Without that, a phone coming back from a tunnel either misses the state change it was waiting
for or re-renders one it already showed, and on a screen whose whole claim is "right now",
both look like the estimate flickering.

**A slow client is dropped, not buffered.** Its queue is bounded; when it fills, the
subscriber is disconnected. Unbounded queues in a fan-out are how one stalled mobile
connection turns into the server's memory problem.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import itertools
import json
import logging
import uuid
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

log = logging.getLogger("haazir.realtime")

# One venue's fan-out. Small on purpose: a subscriber this far behind is not going to catch up.
QUEUE_MAX = 32
# Replayable history per venue. Enough for a tunnel, not enough to be a store.
BACKLOG = 24
# Comment frames keep proxies and mobile networks from closing an idle connection.
KEEPALIVE_SECONDS = 15


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    venue_id: uuid.UUID
    kind: str
    data: dict
    at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def to_sse(self) -> str:
        payload = json.dumps({**self.data, "at": self.at.isoformat()}, default=str)
        return f"id: {self.id}\nevent: {self.kind}\ndata: {payload}\n\n"


class Hub:
    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, set[asyncio.Queue[Event]]] = {}
        self._backlog: dict[uuid.UUID, deque[Event]] = {}
        self._ids = itertools.count(1)

    # --- publishing ---------------------------------------------------------

    def publish(self, venue_id: uuid.UUID, kind: str, data: dict) -> Event:
        """Fan out to everyone watching this venue. Never raises, never blocks.

        A publish happens inside a request that has already done the thing it is announcing,
        so a failure here must not turn a successful staff tap into a 500.
        """
        event = Event(id=next(self._ids), venue_id=venue_id, kind=kind, data=data)

        history = self._backlog.setdefault(venue_id, deque(maxlen=BACKLOG))
        history.append(event)

        dropped = []
        for queue in self._subscribers.get(venue_id, set()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dropped.append(queue)

        for queue in dropped:
            # Disconnect rather than grow. See the module note.
            self._discard(venue_id, queue)
            log.info("dropped a subscriber on %s: queue full", venue_id)

        return event

    # --- subscribing --------------------------------------------------------

    def _discard(self, venue_id: uuid.UUID, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(venue_id)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            del self._subscribers[venue_id]

    def missed_since(self, venue_id: uuid.UUID, last_event_id: int | None) -> list[Event]:
        if last_event_id is None:
            return []
        return [e for e in self._backlog.get(venue_id, ()) if e.id > last_event_id]

    @contextlib.asynccontextmanager
    async def subscribe(self, venue_id: uuid.UUID) -> AsyncIterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_MAX)
        self._subscribers.setdefault(venue_id, set()).add(queue)
        try:
            yield queue
        finally:
            self._discard(venue_id, queue)

    # --- introspection, for tests and the health probe ----------------------

    def subscriber_count(self, venue_id: uuid.UUID | None = None) -> int:
        if venue_id is not None:
            return len(self._subscribers.get(venue_id, ()))
        return sum(len(s) for s in self._subscribers.values())

    def reset(self) -> None:
        self._subscribers.clear()
        self._backlog.clear()


hub = Hub()


async def event_stream(
    venue_id: uuid.UUID, last_event_id: int | None = None
) -> AsyncIterator[str]:
    """SSE frames for one venue: the missed backlog, then live, with keepalives between."""
    async with hub.subscribe(venue_id) as queue:
        for missed in hub.missed_since(venue_id, last_event_id):
            yield missed.to_sse()

        # Tells the browser not to hammer on reconnect, and is the first thing a client can
        # see, so a stream with no traffic yet still proves it is open.
        yield "retry: 3000\n\n"

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            except asyncio.CancelledError:
                break
            yield event.to_sse()


def parse_last_event_id(raw: str | None) -> int | None:
    """`Last-Event-ID` is a client-supplied header, so it is parsed, not trusted."""
    if not raw:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None
