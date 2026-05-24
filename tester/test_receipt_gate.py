"""Tests for the tool-receipt hallucination gate (``tester/``).

Two layers:

* Unit — the gate logic on hand-built event streams (tampered/clean), no I/O.
* Integration — every offline mock scenario is recorded through the bus and
  must back its terminal ``outcome`` with a successful write. This is the
  regression guard: green today, red the moment a refactor lets the FSM emit a
  positive outcome without a successful tool behind it.
"""

from __future__ import annotations

import pytest

from evals.mock_llm import available_mock_scenarios
from evals.scenarios import SCENARIOS
from evals.types import Scenario
from prosper.console.events import ConsoleEvent, make_event
from tester.receipt_gate import Violation, check_receipts
from tester.recorder import record_call


def _outcome(label: str, session_id: str = "s1") -> ConsoleEvent:
    """Build a terminal ``outcome`` event carrying ``label``."""
    return make_event("outcome", session_id, {"outcome": label, "details": {}})


def _tool_end(tool: str, outcome: str = "ok", session_id: str = "s1") -> ConsoleEvent:
    """Build a ``tool_call_end`` event for ``tool`` with the given outcome."""
    return make_event(
        "tool_call_end",
        session_id,
        {"tool": tool, "call_id": "c1", "outcome": outcome, "duration_ms": 1.0},
    )


# --- unit: gate logic on hand-built streams -------------------------------


def test_booked_with_receipt_passes() -> None:
    events = [_tool_end("create_appointment"), _outcome("booked")]
    assert check_receipts(events) == []


def test_booked_without_receipt_flags() -> None:
    violations = check_receipts([_outcome("booked")])
    assert len(violations) == 1
    assert violations[0].expected_tool == "create_appointment"


def test_booked_with_failed_create_flags() -> None:
    # A create that returned Err is NOT a receipt.
    events = [_tool_end("create_appointment", outcome="err"), _outcome("booked")]
    assert len(check_receipts(events)) == 1


def test_cancelled_requires_cancel_tool() -> None:
    assert check_receipts([_outcome("cancelled")])  # missing receipt
    assert check_receipts([_tool_end("cancel_appointment"), _outcome("cancelled")]) == []


def test_rescheduled_requires_reschedule_tool() -> None:
    assert check_receipts([_outcome("rescheduled")])  # missing receipt
    ok = [_tool_end("reschedule_appointment"), _outcome("rescheduled")]
    assert check_receipts(ok) == []


def test_refused_and_abandoned_need_no_receipt() -> None:
    assert check_receipts([_outcome("refused")]) == []
    assert check_receipts([_outcome("abandoned")]) == []


def test_unknown_outcome_is_flagged() -> None:
    # Defensive: a new outcome label with no receipt rule must not pass silently.
    violations = check_receipts([_outcome("teleported")])
    assert len(violations) == 1
    assert violations[0].expected_tool == ""


def test_receipt_must_precede_claim() -> None:
    # A receipt arriving AFTER the outcome claim doesn't count.
    events = [_outcome("booked"), _tool_end("create_appointment")]
    assert len(check_receipts(events)) == 1


def test_receipt_is_per_session() -> None:
    # A success in session A cannot vouch for a claim in session B.
    events = [
        _tool_end("create_appointment", session_id="A"),
        _outcome("booked", session_id="B"),
    ]
    violations = check_receipts(events)
    assert len(violations) == 1
    assert violations[0].session_id == "B"


def test_violation_str_is_one_line() -> None:
    v = Violation("sid", "booked", "create_appointment", "no receipt")
    assert "\n" not in str(v)
    assert "sid" in str(v)


# --- integration: every offline mock scenario must back its outcome --------

_MOCK_NAMES = set(available_mock_scenarios())
_MOCK_SCENARIOS = [s for s in SCENARIOS if s.name in _MOCK_NAMES]


def test_mock_scenarios_discovered() -> None:
    # Guard against the scenario registry and the mock-script table drifting apart.
    assert _MOCK_SCENARIOS, "no mock scenarios discovered — check evals/mock_llm scripts"


@pytest.mark.parametrize("scenario", _MOCK_SCENARIOS, ids=lambda s: s.name)
async def test_mock_scenario_outcome_has_receipt(scenario: Scenario) -> None:
    events = await record_call(scenario)
    violations = check_receipts(events)
    assert not violations, "; ".join(str(v) for v in violations)
