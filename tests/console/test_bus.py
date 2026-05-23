"""Unit tests for `prosper.console.bus.ConsoleBus`.

Covers: subscribe→publish→receive flow, fan-out across N subscribers,
context-managed cleanup (no subscriber leaks), bounded-queue overflow
behaviour (oldest dropped, warning logged once).
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from prosper.console.bus import ConsoleBus
from prosper.console.events import ConsoleEvent, make_event


def _tick(session_id: str = "s", ts: float = 0.0) -> ConsoleEvent:
    """Build a minimal valid `ConsoleEvent` for tests."""
    return make_event(
        "latency_tick",
        session_id=session_id,
        payload={"phase": "llm", "duration_ms": 1.0},
        ts=ts,
    )


@pytest.mark.asyncio
async def test_subscribe_then_publish_delivers_event() -> None:
    """A subscriber registered before publish must receive the event."""
    bus = ConsoleBus()
    async with bus.subscribe() as queue:
        await bus.publish(_tick())
        received = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert received.session_id == "s"


@pytest.mark.asyncio
async def test_publish_before_subscribe_does_not_replay() -> None:
    """The bus is fire-and-forget — late subscribers do not see past events."""
    bus = ConsoleBus()
    await bus.publish(_tick(ts=1.0))
    async with bus.subscribe() as queue:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.1)


@pytest.mark.asyncio
async def test_fan_out_to_multiple_subscribers() -> None:
    """Every active subscriber must receive each published event."""
    bus = ConsoleBus()
    async with bus.subscribe() as q1, bus.subscribe() as q2, bus.subscribe() as q3:
        await bus.publish(_tick())
        for q in (q1, q2, q3):
            received = await asyncio.wait_for(q.get(), timeout=1.0)
            assert received.session_id == "s"


@pytest.mark.asyncio
async def test_subscriber_removed_on_context_exit() -> None:
    """Exiting `async with` must remove the subscriber — no memory leak."""
    bus = ConsoleBus()
    async with bus.subscribe():
        assert bus.subscriber_count == 1
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_subscriber_removed_on_exception() -> None:
    """Even on exception, the subscriber must be cleaned up."""
    bus = ConsoleBus()
    with pytest.raises(RuntimeError):
        async with bus.subscribe():
            assert bus.subscriber_count == 1
            raise RuntimeError("boom")
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_overflow_drops_oldest_and_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    """When a subscriber falls behind, the oldest event must be dropped, not blocked.

    The publisher must remain non-blocking and a single WARNING must be
    emitted per session id (subsequent overflows on the same session
    are silent to avoid log spam — design spec §3.4).
    """
    caplog.set_level(logging.WARNING, logger="prosper.console.bus")
    bus = ConsoleBus(queue_depth=2)
    async with bus.subscribe() as queue:
        # Push 4 events into a depth-2 queue — the publisher must never block.
        for i in range(4):
            await asyncio.wait_for(
                bus.publish(_tick(ts=float(i))),
                timeout=0.5,
            )
        # ts values published are 0.0, 1.0, 2.0, 3.0; depth-2 keeps newest 2.
        assert queue.qsize() == 2
        first = await queue.get()
        second = await queue.get()
        assert {first.ts, second.ts} == {2.0, 3.0}
    overflow_warnings = [r for r in caplog.records if "subscriber overflow" in r.getMessage()]
    assert len(overflow_warnings) == 1, "must log overflow exactly once per session"


@pytest.mark.asyncio
async def test_overflow_warning_is_per_session_not_global(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Distinct session ids each get their own first-overflow warning."""
    caplog.set_level(logging.WARNING, logger="prosper.console.bus")
    bus = ConsoleBus(queue_depth=1)
    async with bus.subscribe() as _q:
        for sid in ("s-a", "s-b"):
            for i in range(3):  # force overflow on each session
                await bus.publish(_tick(session_id=sid, ts=float(i)))
    overflow_warnings = [r for r in caplog.records if "subscriber overflow" in r.getMessage()]
    assert len(overflow_warnings) == 2


def test_queue_depth_must_be_positive() -> None:
    """A zero/negative depth would silently break publication — reject early."""
    with pytest.raises(ValueError, match="queue_depth"):
        ConsoleBus(queue_depth=0)
