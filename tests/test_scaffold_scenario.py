"""Unit tests for the scenario scaffolder (FUTURE.md 6.2).

Pins the heuristic inference: a booking transcript yields a +1 active delta
and a create_appointment expectation, a cancellation yields the cancel deltas,
and the emitted stub is syntactically plausible Python carrying the right
TODO markers.
"""

from __future__ import annotations

import sys
from pathlib import Path

# scripts/ is not a package; add it to the path for import.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scaffold_scenario import scaffold

_BOOKING = """\
BOT: Hi, thanks for calling Prosper Health.
USER: I'd like to book an appointment.
USER: 202-555-0100.
BOT: Found you, Ada.
USER: Book tomorrow morning please.
BOT: I have ten o'clock — shall I book?
USER: Yes please.
BOT: You're all set for tomorrow at ten. Have a great day.
"""

_CANCELLATION = """\
USER: I need to cancel my appointment.
USER: 202-555-0100.
BOT: You have one visit Tuesday at ten — cancel it?
USER: Yes, cancel it.
BOT: That's cancelled. Goodbye.
"""

_NEW_PATIENT = """\
USER: I'm a new patient.
USER: 555-111-2222.
USER: Sam Rivera, March 3rd 1990.
BOT: I'll register Sam Rivera. You're registered.
USER: Book tomorrow.
BOT: Booked for tomorrow at nine.
"""


def test_booking_infers_active_delta_and_create_tool() -> None:
    stub = scaffold(_BOOKING, name="my_booking")
    assert "active_appointment_count_delta=1" in stub
    assert "create_appointment" in stub
    assert 'expected_terminal_state="END"' in stub
    assert 'name="my_booking"' in stub


def test_cancellation_infers_cancel_deltas() -> None:
    stub = scaffold(_CANCELLATION)
    assert "cancelled_appointment_count_delta=1" in stub
    assert "active_appointment_count_delta=-1" in stub
    assert "cancel_appointment" in stub


def test_new_patient_infers_patient_delta_and_create_patient() -> None:
    stub = scaffold(_NEW_PATIENT)
    assert "patient_count_delta=1" in stub
    assert "create_patient" in stub
    assert "create_appointment" in stub  # also booked


def test_stub_carries_todo_markers_for_uninferable_fields() -> None:
    stub = scaffold(_BOOKING)
    assert "_TODO_setup" in stub  # DB seed can't be inferred from text
    assert "TODO" in stub  # tags / judge criteria
    assert "Scenario(" in stub and "StateExpectation(" in stub


def test_max_turns_scales_with_user_turn_count() -> None:
    # _BOOKING has 4 USER turns -> max(8, 4+4) == 8.
    assert "max_turns=8" in scaffold(_BOOKING)


def test_empty_transcript_yields_safe_todo_stub() -> None:
    stub = scaffold("")
    assert "TODO" in stub
    assert "active_appointment_count_delta=0" in stub
