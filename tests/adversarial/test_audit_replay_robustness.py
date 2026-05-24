"""Adversarial: the durable replay path must survive a corrupt audit file.

A mid-write crash leaves a truncated final JSONL line; an operator replay
(`audit.iter_events`) must skip malformed/blank lines and still yield every
intact event, never raise. Also round-trips the newest event type so the
on-disk format stays decodable.
"""

from __future__ import annotations

from prosper.console.audit import AuditJSONLWriter
from prosper.console.events import ConsoleEvent, make_event


async def test_iter_events_skips_malformed_and_blank_lines(tmp_path) -> None:
    writer = AuditJSONLWriter(root=tmp_path)
    good1 = make_event(
        "latency_tick", session_id="s1", payload={"phase": "llm", "duration_ms": 1.0}, ts=1.0
    )
    good2 = make_event(
        "outcome", session_id="s1", payload={"outcome": "booked", "details": {}}, ts=2.0
    )
    # Hand-craft a file: valid, blank, garbage, half-written (truncated), valid.
    path = writer.path_for("s1")
    path.write_text(
        good1.to_json()
        + "\n\n"
        + "this is not json at all\n"
        + '{"type":"outcome","ts":3.0,"sess'  # truncated mid-write, no newline
        + "\n"
        + good2.to_json()
        + "\n",
        encoding="utf-8",
    )

    recovered = [ev async for ev in writer.iter_events("s1")]
    assert [e.ts for e in recovered] == [1.0, 2.0]
    assert [e.type for e in recovered] == ["latency_tick", "outcome"]


async def test_iter_events_on_missing_session_is_empty(tmp_path) -> None:
    writer = AuditJSONLWriter(root=tmp_path)
    assert [ev async for ev in writer.iter_events("never-existed")] == []


async def test_turn_interrupted_round_trips_through_jsonl(tmp_path) -> None:
    """The newest event type must survive write → iter_events unchanged."""
    writer = AuditJSONLWriter(root=tmp_path)
    event = make_event(
        "turn_interrupted",
        session_id="s2",
        payload={"turn_id": 4, "state": "CONFIRM_BOOK", "spoken_text": "Booking 2 PM with"},
        ts=9.0,
    )
    await writer.write(event)
    await writer.close()
    recovered = [ev async for ev in writer.iter_events("s2")]
    assert recovered == [event]


def test_from_json_rejects_non_json() -> None:
    """`from_json` raises on garbage so `iter_events` can skip-and-continue."""
    import pytest

    with pytest.raises(ValueError):
        ConsoleEvent.from_json("definitely not json {{{")
