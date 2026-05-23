"""Typed telemetry events for the Operator Console.

A single frozen dataclass (`ConsoleEvent`) carries every event published on
the bus. The `type` field is a closed string set (`EVENT_TYPES`) — see the
design spec §3.3 for the rationale (testable, replayable, forward-compat).

Each event has a `payload` shape documented per type in `_REQUIRED_KEYS`.
`validate_event` enforces the shape at the publish boundary so a malformed
event cannot reach the audit JSONL or the browser.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Final, Literal, get_args

EventType = Literal[
    "state_change",
    "tool_call_start",
    "tool_call_end",
    "patient_identified",
    "slots_offered",
    "transcript_turn",
    "outcome",
    "latency_tick",
]

# Derived from `EventType` via `get_args` — single source of truth. Adding a
# 9th type means editing `EventType` and `_REQUIRED_KEYS` only; the runtime
# set updates automatically and cannot drift.
EVENT_TYPES: Final[frozenset[EventType]] = frozenset(get_args(EventType))

# Per-type minimum payload contract. A payload may carry extra keys; missing
# required keys raise. Keep these in sync with §3.3 of the design spec — the
# spec is the source of truth, this dict mirrors it.
_REQUIRED_KEYS: Final[dict[EventType, frozenset[str]]] = {
    "state_change": frozenset({"from_state", "to_state", "trigger"}),
    "tool_call_start": frozenset({"tool", "args_redacted", "call_id"}),
    "tool_call_end": frozenset({"tool", "call_id", "outcome", "duration_ms"}),
    "patient_identified": frozenset({"name_masked", "dob_year", "phone_masked", "id_internal"}),
    "slots_offered": frozenset({"count", "providers"}),
    "transcript_turn": frozenset({"role", "text", "turn_id"}),
    "outcome": frozenset({"outcome", "details"}),
    "latency_tick": frozenset({"phase", "duration_ms"}),
}

# Defence-in-depth: any field whose name ends with `_masked` must contain at
# least one masking character. The publisher is supposed to redact before
# building the event (see `observability/redact.py`); this check is the
# bus-level safety net so a forgotten redaction surfaces as a hard ValueError
# rather than leaking raw PII to the audit log or the browser.
_MASK_CHARS: Final[frozenset[str]] = frozenset({"*", "X", "x", "•", "#"})
# Heuristic: 7+ consecutive digits in a `_masked` field signals an unmasked
# phone number. Cheaper than a full regex of every PII shape, and any false
# positive (e.g. a long appointment id) would itself be a spec violation.
_UNMASKED_DIGIT_RUN: Final[re.Pattern[str]] = re.compile(r"\d{7,}")


@dataclass(frozen=True, slots=True)
class ConsoleEvent:
    """One telemetry event published by the dispatcher to the operator console.

    Attributes:
        type: One of the strings in `EVENT_TYPES`.
        ts: `time.time()` at the moment of publication (UTC epoch seconds).
        session_id: Stable id for the call/eval session this event belongs to.
        payload: Per-type structured data; see `_REQUIRED_KEYS` for the
            minimum shape per event type.

    The class is `frozen=True, slots=True` — events are immutable telemetry
    facts; mutating one after publication would invalidate the audit trail.
    """

    type: EventType
    ts: float
    session_id: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialise to a one-line JSON string (the JSONL on-disk format)."""
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> ConsoleEvent:
        """Inverse of `to_json`. Raises on invalid JSON or missing fields."""
        data = json.loads(raw)
        return cls(
            type=data["type"],
            ts=float(data["ts"]),
            session_id=str(data["session_id"]),
            payload=dict(data.get("payload", {})),
        )


def make_event(
    type: EventType,
    session_id: str,
    payload: dict[str, Any] | None = None,
    *,
    ts: float | None = None,
) -> ConsoleEvent:
    """Build a `ConsoleEvent` with sensible defaults.

    `ts` defaults to `time.time()` when omitted; tests pass an explicit value
    for determinism. `payload` defaults to `{}` so the caller can omit it for
    payload-less events (none exist today, but the API supports it).

    Validation runs eagerly — a malformed event raises `ValueError` here,
    before it can reach the bus.
    """
    event = ConsoleEvent(
        type=type,
        ts=time.time() if ts is None else ts,
        session_id=session_id,
        payload=payload or {},
    )
    validate_event(event)
    return event


def validate_event(event: ConsoleEvent) -> None:
    """Raise `ValueError` if `event` violates its per-type payload contract.

    Defence in depth: callers should also build events through `make_event`
    which calls this. Direct dataclass construction is allowed (cheap), but
    skipping validation is the caller's choice.

    Three checks run in order:
      1. Event type is in the closed `EVENT_TYPES` set.
      2. `session_id` is non-empty.
      3. Per-type required payload keys are present.
      4. Any field whose key ends with `_masked` carries at least one mask
         character AND does not contain a 7+ digit run (would indicate a
         raw phone number leaking through). See spec §9 "Why redact at the
         event boundary".
    """
    if event.type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {event.type!r}")
    if not event.session_id:
        raise ValueError("session_id must be a non-empty string")
    required = _REQUIRED_KEYS[event.type]
    missing = required - event.payload.keys()
    if missing:
        raise ValueError(
            f"event type {event.type!r} missing required payload keys: {sorted(missing)}"
        )
    _check_masked_fields(event)


def _check_masked_fields(event: ConsoleEvent) -> None:
    """Raise if any `*_masked` field looks unredacted.

    The publisher is responsible for redaction; this is the bus-level
    safety net that converts a forgotten redaction into a noisy error.
    """
    for key, value in event.payload.items():
        if not key.endswith("_masked"):
            continue
        if not isinstance(value, str):
            raise ValueError(
                f"event type {event.type!r}: payload[{key!r}] must be a "
                f"redacted string, got {type(value).__name__}"
            )
        if not any(ch in _MASK_CHARS for ch in value):
            raise ValueError(
                f"event type {event.type!r}: payload[{key!r}]={value!r} "
                f"contains no masking character — looks unredacted"
            )
        if _UNMASKED_DIGIT_RUN.search(value):
            raise ValueError(
                f"event type {event.type!r}: payload[{key!r}]={value!r} "
                f"contains a 7+ digit run — looks like raw phone number"
            )
