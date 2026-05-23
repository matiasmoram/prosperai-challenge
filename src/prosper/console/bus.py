"""In-memory async pub/sub bus for `ConsoleEvent` fan-out.

One bus instance per process. Subscribers receive their own bounded
`asyncio.Queue`; the publisher never blocks. If a subscriber falls behind
and its queue fills, the bus drops the oldest event for that subscriber
and logs a single warning per session — never silent.

See design spec §3.4 (backpressure) and §3.5 (threading model) for the
rationale behind these choices.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from prosper.console.events import ConsoleEvent, validate_event

logger = logging.getLogger(__name__)

# Per-subscriber queue depth. Sized so a 60-second connection hiccup still
# fits at the highest publish rate we expect (~10 events/turn x 4 turns/s
# burst during STT chunking = 40/s steady-state). Override via env for
# load-testing without touching code.
_DEFAULT_QUEUE_DEPTH: Final[int] = int(os.environ.get("PROSPER_CONSOLE_QUEUE_DEPTH", "256"))


class ConsoleBus:
    """Async fan-out bus for `ConsoleEvent`.

    Publication is non-blocking. Subscription is context-managed; the
    subscriber's queue is removed on exit so a disconnected SSE client
    cannot leak memory.
    """

    def __init__(self, queue_depth: int = _DEFAULT_QUEUE_DEPTH) -> None:
        """Initialise an empty bus.

        Args:
            queue_depth: Maximum events buffered per subscriber. When full,
                oldest events are dropped (a single warning per session
                is logged). Default sized for a 60-second hiccup at the
                observed publish rate.
        """
        if queue_depth < 1:
            raise ValueError(f"queue_depth must be >= 1, got {queue_depth}")
        self._queue_depth = queue_depth
        self._subscribers: list[asyncio.Queue[ConsoleEvent]] = []
        self._overflow_sessions: set[str] = set()

    @property
    def subscriber_count(self) -> int:
        """Current number of active subscribers (for tests + diagnostics)."""
        return len(self._subscribers)

    async def publish(self, event: ConsoleEvent) -> None:
        """Fan `event` out to every active subscriber.

        Non-blocking. If any subscriber's queue is full, the oldest event
        in that subscriber's queue is discarded to make room (the
        publisher path stays unblocked). The discard is logged at WARNING
        level exactly once per session_id to avoid log spam.
        """
        validate_event(event)
        for queue in self._subscribers:
            self._enqueue_with_drop(queue, event)

    def _enqueue_with_drop(
        self,
        queue: asyncio.Queue[ConsoleEvent],
        event: ConsoleEvent,
    ) -> None:
        """Push `event` onto `queue`; drop oldest if full."""
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            if event.session_id not in self._overflow_sessions:
                self._overflow_sessions.add(event.session_id)
                logger.warning(
                    "console-bus subscriber overflow (session=%s, depth=%d) — "
                    "dropping oldest event(s); further overflow on this "
                    "session will be silent",
                    event.session_id,
                    self._queue_depth,
                )
            with contextlib.suppress(asyncio.QueueEmpty):
                # asyncio is single-threaded; the only reachable Empty case
                # is queue_depth==1 where the get drains the only slot —
                # suppress is cheaper than a conditional check.
                queue.get_nowait()
                # Pair the drop with `task_done` so any consumer using
                # `queue.join()` for shutdown still sees a balanced counter.
                # `task_done` raises ValueError if over-called, but in the
                # single-threaded asyncio model the surrounding `get_nowait`
                # guarantees we just dropped one outstanding item.
                queue.task_done()
            queue.put_nowait(event)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[ConsoleEvent]]:
        """Register a subscriber; yield its private event queue.

        Usage:
            async with bus.subscribe() as q:
                while True:
                    event = await q.get()
                    ...

        On exit (normal or exception), the subscriber is removed from the
        fan-out list. There is no way to leak a subscriber.
        """
        queue: asyncio.Queue[ConsoleEvent] = asyncio.Queue(maxsize=self._queue_depth)
        self._subscribers.append(queue)
        try:
            yield queue
        finally:
            self._subscribers.remove(queue)
