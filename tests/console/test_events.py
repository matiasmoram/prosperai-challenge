"""Unit tests for `prosper.console.events`.

Covers: round-trip JSON serialisation, validation of every event type,
and the safety contract of `make_event` (eager validation, deterministic ts).
"""

from __future__ import annotations

import pytest

from prosper.console.events import (
    EVENT_TYPES,
    ConsoleEvent,
    make_event,
    validate_event,
)


def test_event_types_set_is_frozen_and_complete() -> None:
    """The closed event-type set must contain exactly the 9 documented types."""
    assert len(EVENT_TYPES) == 9
    expected = {
        "state_change",
        "tool_call_start",
        "tool_call_end",
        "patient_identified",
        "slots_offered",
        "transcript_turn",
        "outcome",
        "latency_tick",
        "turn_interrupted",
    }
    assert set(EVENT_TYPES) == expected


def test_round_trip_serialisation_preserves_payload() -> None:
    """`to_json` then `from_json` must reproduce the original event exactly."""
    original = make_event(
        "state_change",
        session_id="sess-42",
        payload={"from_state": "GREETING", "to_state": "IDENTIFY_PATIENT", "trigger": "greeted"},
        ts=1716200000.5,
    )
    restored = ConsoleEvent.from_json(original.to_json())
    assert restored == original


def test_to_json_is_single_line() -> None:
    """The JSONL on-disk format requires one event per line — no embedded newlines."""
    event = make_event(
        "outcome",
        session_id="s1",
        payload={"outcome": "booked", "details": {"provider": "Patel"}},
        ts=1.0,
    )
    serialised = event.to_json()
    assert "\n" not in serialised
    assert serialised.startswith("{")


@pytest.mark.parametrize(
    "event_type,payload",
    [
        ("state_change", {"from_state": "A", "to_state": "B", "trigger": "x"}),
        ("tool_call_start", {"tool": "find_x", "args_redacted": {}, "call_id": "c1"}),
        ("tool_call_end", {"tool": "find_x", "call_id": "c1", "outcome": "ok", "duration_ms": 1.0}),
        (
            "patient_identified",
            {
                "name_masked": "S** M.****",
                "dob_year": 1988,
                "phone_masked": "+1***0142",
                "id_internal": "uuid-1",
            },
        ),
        ("slots_offered", {"count": 2, "providers": ["Patel"]}),
        ("transcript_turn", {"role": "user", "text": "hi", "turn_id": 1}),
        ("outcome", {"outcome": "booked", "details": {}}),
        ("latency_tick", {"phase": "llm", "duration_ms": 420.0}),
    ],
)
def test_validate_accepts_minimum_payload_for_each_type(
    event_type: str, payload: dict[str, object]
) -> None:
    """Every event type must accept its documented minimum payload."""
    event = ConsoleEvent(type=event_type, ts=0.0, session_id="s", payload=payload)  # type: ignore[arg-type]
    validate_event(event)  # must not raise


def test_validate_rejects_unknown_event_type() -> None:
    """A typo in the `type` field must raise — defends the front-end contract."""
    bad = ConsoleEvent(type="not_a_real_type", ts=0.0, session_id="s", payload={})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown event type"):
        validate_event(bad)


def test_validate_rejects_empty_session_id() -> None:
    """Every event must be attributable to a session."""
    bad = ConsoleEvent(
        type="latency_tick",
        ts=0.0,
        session_id="",
        payload={"phase": "llm", "duration_ms": 1.0},
    )
    with pytest.raises(ValueError, match="session_id"):
        validate_event(bad)


def test_validate_rejects_missing_required_payload_key() -> None:
    """Per-type required keys must be enforced — protects audit contract."""
    bad = ConsoleEvent(
        type="state_change",
        ts=0.0,
        session_id="s",
        payload={"from_state": "A"},  # missing to_state + trigger
    )
    with pytest.raises(ValueError, match="missing required payload keys"):
        validate_event(bad)


def test_validate_rejects_unredacted_phone_in_masked_field() -> None:
    """`*_masked` field with a 7+ digit run signals a raw phone leak."""
    bad = ConsoleEvent(
        type="patient_identified",
        ts=0.0,
        session_id="s",
        payload={
            "name_masked": "S** M.****",
            "dob_year": 1988,
            "phone_masked": "+*2025550142",  # has a mask but still leaks 10 digits
            "id_internal": "uuid",
        },
    )
    with pytest.raises(ValueError, match="raw phone number"):
        validate_event(bad)


def test_validate_rejects_masked_field_without_mask_chars() -> None:
    """A `_masked` field carrying letters but no `*` indicates redaction missed."""
    bad = ConsoleEvent(
        type="patient_identified",
        ts=0.0,
        session_id="s",
        payload={
            "name_masked": "Sarah Mendez",  # no mask char at all
            "dob_year": 1988,
            "phone_masked": "+1***0142",
            "id_internal": "uuid",
        },
    )
    with pytest.raises(ValueError, match="no masking character"):
        validate_event(bad)


def test_validate_rejects_non_string_masked_field() -> None:
    """`_masked` fields must be strings — non-string suggests bypass attempt."""
    bad = ConsoleEvent(
        type="patient_identified",
        ts=0.0,
        session_id="s",
        payload={
            "name_masked": 42,  # type error — should be a redacted string
            "dob_year": 1988,
            "phone_masked": "+1***0142",
            "id_internal": "uuid",
        },
    )
    with pytest.raises(ValueError, match="redacted string"):
        validate_event(bad)


def test_event_types_set_is_derived_from_literal() -> None:
    """`EVENT_TYPES` must be derived from `EventType` so the two cannot drift.

    A future contributor must only edit `EventType` (and `_REQUIRED_KEYS`) —
    the frozenset updates automatically via `typing.get_args`.
    """
    from typing import get_args

    from prosper.console.events import EventType

    assert frozenset(get_args(EventType)) == EVENT_TYPES


def test_make_event_validates_eagerly() -> None:
    """`make_event` must reject bad payloads at construction, not at publish."""
    with pytest.raises(ValueError):
        make_event("state_change", session_id="s", payload={})


def test_make_event_explicit_ts_is_deterministic() -> None:
    """Passing `ts` explicitly disables wall-clock — needed for reproducible tests."""
    e1 = make_event(
        "latency_tick",
        session_id="s",
        payload={"phase": "llm", "duration_ms": 1.0},
        ts=7.0,
    )
    e2 = make_event(
        "latency_tick",
        session_id="s",
        payload={"phase": "llm", "duration_ms": 1.0},
        ts=7.0,
    )
    assert e1.ts == e2.ts == 7.0


def test_event_is_immutable() -> None:
    """`frozen=True` is load-bearing — audit events must not mutate post-publish."""
    from dataclasses import FrozenInstanceError

    event = make_event(
        "latency_tick",
        session_id="s",
        payload={"phase": "llm", "duration_ms": 1.0},
        ts=0.0,
    )
    with pytest.raises(FrozenInstanceError):
        event.session_id = "different"  # type: ignore[misc]
