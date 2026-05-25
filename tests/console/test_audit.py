"""Unit tests for `prosper.console.audit.AuditJSONLWriter`.

Covers: write→read round-trip, path-traversal defence, replay of malformed
lines, attach-to-bus drain semantics, and graceful close on shutdown.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from prosper.console.audit import AuditJSONLWriter
from prosper.console.bus import ConsoleBus
from prosper.console.events import ConsoleEvent, make_event


def _tick(session_id: str = "s1", ts: float = 0.0) -> ConsoleEvent:
    """Build a `latency_tick` event with deterministic timestamp."""
    return make_event(
        "latency_tick",
        session_id=session_id,
        payload={"phase": "llm", "duration_ms": 1.0},
        ts=ts,
    )


@pytest.mark.asyncio
async def test_write_then_read_round_trip(tmp_path: Path) -> None:
    """An event written must replay back as the exact same `ConsoleEvent`."""
    writer = AuditJSONLWriter(root=tmp_path)
    event = _tick(ts=42.0)
    await writer.write(event)
    await writer.close()

    restored = [e async for e in writer.iter_events("s1")]
    assert restored == [event]


@pytest.mark.asyncio
async def test_multiple_events_preserve_order(tmp_path: Path) -> None:
    """JSONL is append-only; replay must yield events in write order."""
    writer = AuditJSONLWriter(root=tmp_path)
    events = [_tick(ts=float(i)) for i in range(5)]
    for e in events:
        await writer.write(e)
    await writer.close()

    restored = [e async for e in writer.iter_events("s1")]
    assert restored == events


@pytest.mark.asyncio
async def test_replay_skips_malformed_lines(tmp_path: Path) -> None:
    """A truncated mid-write must not abort the entire replay."""
    writer = AuditJSONLWriter(root=tmp_path)
    good = _tick(ts=1.0)
    await writer.write(good)
    await writer.close()

    # Inject a malformed line between two good ones.
    path = writer.path_for("s1")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json}\n")
        fh.write(_tick(ts=3.0).to_json() + "\n")

    restored = [e async for e in writer.iter_events("s1")]
    assert [e.ts for e in restored] == [1.0, 3.0]


@pytest.mark.asyncio
async def test_iter_events_missing_file_yields_nothing(tmp_path: Path) -> None:
    """Replay of a never-written session must be a no-op, not an error."""
    writer = AuditJSONLWriter(root=tmp_path)
    restored = [e async for e in writer.iter_events("never-existed")]
    assert restored == []


@pytest.mark.asyncio
async def test_list_sessions_lists_existing_files(tmp_path: Path) -> None:
    """`list_sessions` must include every session that has a `.jsonl`."""
    writer = AuditJSONLWriter(root=tmp_path)
    for sid in ("alpha", "beta", "gamma"):
        await writer.write(_tick(session_id=sid))
    await writer.close()

    assert writer.list_sessions() == ["alpha", "beta", "gamma"]


def test_list_sessions_when_root_missing(tmp_path: Path) -> None:
    """If the audit root has not yet been created, return an empty list."""
    writer = AuditJSONLWriter(root=tmp_path / "does-not-exist")
    assert writer.list_sessions() == []


@pytest.mark.parametrize(
    "evil_session_id",
    [
        "../etc",
        "foo/bar",
        "C:\\Windows\\System32",
        "..\\evil",
        "",
        "with space",
        "trailing.dot",
    ],
)
def test_path_for_rejects_unsafe_session_ids(tmp_path: Path, evil_session_id: str) -> None:
    """`session_id` must be alphanumerics/hyphens/underscores only — defence
    against directory traversal in the path composition."""
    writer = AuditJSONLWriter(root=tmp_path)
    with pytest.raises(ValueError, match="invalid session_id"):
        writer.path_for(evil_session_id)


@pytest.mark.asyncio
async def test_attach_drains_bus_into_audit(tmp_path: Path) -> None:
    """`attach` must wire the bus to disk for the lifetime of the context."""
    writer = AuditJSONLWriter(root=tmp_path)
    bus = ConsoleBus()
    published = [_tick(ts=float(i)) for i in range(3)]

    async with writer.attach(bus):
        for event in published:
            await bus.publish(event)
        # Give the drain task a tick to flush.
        await asyncio.sleep(0.05)

    restored = [e async for e in writer.iter_events("s1")]
    assert [e.ts for e in restored] == [0.0, 1.0, 2.0]


@pytest.mark.asyncio
async def test_close_is_idempotent(tmp_path: Path) -> None:
    """Calling close twice must not raise — supports defensive shutdown."""
    writer = AuditJSONLWriter(root=tmp_path)
    await writer.write(_tick())
    await writer.close()
    await writer.close()  # second close: must be a no-op


@pytest.mark.asyncio
async def test_writes_separate_files_per_session(tmp_path: Path) -> None:
    """Different `session_id`s must land in distinct `.jsonl` files."""
    writer = AuditJSONLWriter(root=tmp_path)
    await writer.write(_tick(session_id="alpha"))
    await writer.write(_tick(session_id="beta"))
    await writer.close()

    assert writer.path_for("alpha").exists()
    assert writer.path_for("beta").exists()
    assert writer.path_for("alpha") != writer.path_for("beta")


@pytest.mark.asyncio
async def test_attach_flushes_publish_at_context_exit(tmp_path: Path) -> None:
    """A publish in the final microsecond of `attach` must still land.

    The race window the implementation closes via `queue.join` — without
    the join, the drain task would be cancelled before processing the
    last event. This test asserts the published event survives.
    """
    writer = AuditJSONLWriter(root=tmp_path)
    bus = ConsoleBus()
    async with writer.attach(bus):
        # No sleep, no awaits between publish and context exit — this is
        # the exact race the join-on-exit closes.
        await bus.publish(_tick(ts=99.0))
    restored = [e async for e in writer.iter_events("s1")]
    assert [e.ts for e in restored] == [99.0]


@pytest.mark.asyncio
async def test_fsync_called_only_for_outcome_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fsync` must run on `outcome` (durability-critical) and only then.

    The other 7 event types tolerate writeback lag — fsync on every
    event would burn IOPS without buying any meaningful guarantee.
    """
    fsync_calls: list[int] = []

    def fake_fsync(fd: int) -> None:
        fsync_calls.append(fd)

    monkeypatch.setattr("prosper.console.audit.os.fsync", fake_fsync)
    writer = AuditJSONLWriter(root=tmp_path)
    await writer.write(_tick(ts=1.0))  # latency_tick — no fsync
    outcome_event = make_event(
        "outcome",
        session_id="s1",
        payload={"outcome": "booked", "details": {"provider": "Patel"}},
        ts=2.0,
    )
    await writer.write(outcome_event)
    await writer.close()
    assert len(fsync_calls) == 1, "fsync must fire exactly once — on the outcome"


@pytest.mark.asyncio
async def test_concurrent_writes_same_session_do_not_interleave(tmp_path: Path) -> None:
    """The write lock must serialise writes to the same session file.

    Two concurrent `write()` calls must produce two complete JSON lines,
    not one corrupted line spliced from both payloads.
    """
    writer = AuditJSONLWriter(root=tmp_path)
    a = _tick(ts=1.0)
    b = _tick(ts=2.0)
    await asyncio.gather(writer.write(a), writer.write(b))
    await writer.close()
    restored = [e async for e in writer.iter_events("s1")]
    assert sorted(e.ts for e in restored) == [1.0, 2.0]
    raw = writer.path_for("s1").read_text(encoding="utf-8").splitlines()
    assert len(raw) == 2, "both writes must produce exactly one line each"
    for line in raw:
        # If the lock failed, a line would be a half-spliced JSON blob.
        ConsoleEvent.from_json(line)


# ---- live-tail (`tail_events`) — backs the SSE follow endpoint -------------


@pytest.mark.asyncio
async def test_tail_events_missing_file_returns_empty(tmp_path: Path) -> None:
    """Tailing a session before its first event is written is a no-op.

    The follower starts polling the instant the call begins; the first event
    may not have flushed yet. That must yield no events and not advance the
    cursor, so the very first event is delivered on a later poll.
    """
    writer = AuditJSONLWriter(root=tmp_path)
    events, next_line = await writer.tail_events("never-written", from_line=0)
    assert events == []
    assert next_line == 0


@pytest.mark.asyncio
async def test_tail_events_incremental_delivery(tmp_path: Path) -> None:
    """Each poll returns only the lines appended since the prior `next_line`.

    Simulates the follow loop against a file the writer keeps appending to:
    first poll gets the backlog, a second immediate poll gets nothing, and a
    poll after a fresh write gets exactly the new event.
    """
    writer = AuditJSONLWriter(root=tmp_path)
    await writer.write(_tick(ts=1.0))
    await writer.write(_tick(ts=2.0))

    batch1, cursor = await writer.tail_events("s1", from_line=0)
    assert [e.ts for e in batch1] == [1.0, 2.0]
    assert cursor == 2

    # No new lines → nothing delivered, cursor unchanged.
    batch2, cursor = await writer.tail_events("s1", from_line=cursor)
    assert batch2 == []
    assert cursor == 2

    # A new write appears on the next poll.
    await writer.write(_tick(ts=3.0))
    batch3, cursor = await writer.tail_events("s1", from_line=cursor)
    assert [e.ts for e in batch3] == [3.0]
    assert cursor == 3
    await writer.close()


@pytest.mark.asyncio
async def test_tail_events_torn_final_line_is_retried(tmp_path: Path) -> None:
    """A final line without a trailing newline (mid-flush) is NOT consumed.

    The cursor must not advance past a torn line, so once the writer appends
    the newline the complete event is delivered on the next poll — never
    dropped, never double-counted.
    """
    writer = AuditJSONLWriter(root=tmp_path)
    await writer.write(_tick(ts=1.0))
    # Append a complete event WITHOUT the trailing newline to mimic a write
    # caught mid-flush (the writer flushes the JSON then the "\n" separately).
    path = writer.path_for("s1")
    torn = _tick(ts=2.0).to_json()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(torn)  # no newline yet

    batch1, cursor = await writer.tail_events("s1", from_line=0)
    assert [e.ts for e in batch1] == [1.0], "torn line must not be delivered"
    assert cursor == 1, "cursor must stop before the torn line"

    # Writer completes the line (adds the newline).
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n")

    batch2, cursor = await writer.tail_events("s1", from_line=cursor)
    assert [e.ts for e in batch2] == [2.0], "the now-complete line is delivered once"
    assert cursor == 2
    await writer.close()
