"""Adversarial: the global console bus can evict a quiet session's event.

Finding F-010. `ConsoleBus` gives each subscriber ONE bounded queue that
receives *every* session's events; `sse._live_event_iter` filters by
`session_id` only AFTER dequeuing. Because the queue is shared across all
sessions and drops the OLDEST event on overflow, a chatty session's burst can
silently evict a quieter watched session's event before that session's SSE
consumer ever dequeues it. For the 1-2 concurrent calls of the demo this never
triggers; under real multi-session load a watched session can lose telemetry.
"""

from __future__ import annotations

from prosper.console.bus import ConsoleBus
from prosper.console.events import make_event


def _evt(session_id: str, turn: int):
    return make_event(
        "transcript_turn",
        session_id=session_id,
        payload={"role": "bot", "text": f"t{turn}", "turn_id": turn},
        ts=float(turn),
    )


async def test_chatty_session_does_not_evict_quiet_session_event() -> None:
    """F-010 fixed: a session-scoped subscriber never buffers another session's
    events, so a chatty burst cannot evict the quiet watched event."""
    bus = ConsoleBus(queue_depth=4)
    async with bus.subscribe("watched") as q:
        await bus.publish(_evt("watched", 0))  # the one event we care about
        for i in range(1, 5):  # chatty burst — filtered out, never enqueued
            await bus.publish(_evt("chatty", i))

        drained = []
        while not q.empty():
            drained.append(q.get_nowait())

    sessions = [e.session_id for e in drained]
    assert sessions == ["watched"], f"watched event lost / chatty leaked: {sessions}"


async def test_unfiltered_subscriber_still_receives_all_sessions() -> None:
    """A ``None`` filter (the audit drain) still sees every session's events."""
    bus = ConsoleBus(queue_depth=16)
    async with bus.subscribe() as q:
        await bus.publish(_evt("a", 0))
        await bus.publish(_evt("b", 1))
        drained = []
        while not q.empty():
            drained.append(q.get_nowait())
    assert sorted(e.session_id for e in drained) == ["a", "b"]


async def test_single_session_within_depth_is_never_dropped() -> None:
    """Contrast: events within the queue depth for one session all survive."""
    bus = ConsoleBus(queue_depth=4)
    async with bus.subscribe("solo") as q:
        for i in range(4):
            await bus.publish(_evt("solo", i))
        drained = []
        while not q.empty():
            drained.append(q.get_nowait())
    assert [e.payload["turn_id"] for e in drained] == [0, 1, 2, 3]
