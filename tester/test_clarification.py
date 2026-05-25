"""Unit tests for the clarification contract (plowed_ahead_on_garble)."""

from __future__ import annotations

from prosper.prompts import FALLBACK_LINES
from tester.clarification import (
    GarbleLog,
    Turn,
    check_recovers_gracefully,
    detect_clarification,
)


def test_detect_clarification_matches_real_fallback_lines() -> None:
    # The detector must recognise the actual re-prompt copy shipped in prompts.
    assert detect_clarification(FALLBACK_LINES["llm_loop_exhausted"])
    assert detect_clarification(FALLBACK_LINES["dispatcher_crash"])


def test_detect_clarification_recognises_shapes() -> None:
    for line in (
        "Which appointment would you like to cancel?",
        "Sorry, did you say Wednesday or Thursday?",
        "Just to confirm, that's the fifteenth?",
        "Could you spell your last name for me?",
    ):
        assert detect_clarification(line), line


def test_detect_clarification_false_on_plain_confirmation() -> None:
    assert not detect_clarification("Great — you're booked for Tuesday at 9 with Dr. Lee.")
    assert not detect_clarification("Okay, I've cancelled that appointment for you.")


def test_write_on_garbled_turn_without_clarification_is_violation() -> None:
    turns = [
        Turn(
            role="caller",
            text="book me december fifty",
            garbled=True,
            original="december fifteenth",
        ),
        Turn(role="bot", text="Done — booked for December.", wrote="create_appointment"),
    ]
    violations = check_recovers_gracefully(turns)
    assert len(violations) == 1
    assert violations[0].kind == "plowed_ahead_on_garble"


def test_clarification_before_write_clears_the_garble() -> None:
    turns = [
        Turn(
            role="caller",
            text="book me december fifty",
            garbled=True,
            original="december fifteenth",
        ),
        Turn(role="bot", text="Sorry, did you say the fifteenth or the fiftieth?"),
        Turn(role="caller", text="the fifteenth"),
        Turn(role="bot", text="Great, booked for the fifteenth.", wrote="create_appointment"),
    ]
    assert check_recovers_gracefully(turns) == []


def test_write_on_clean_turn_is_fine() -> None:
    turns = [
        Turn(role="caller", text="book december fifteenth", garbled=False),
        Turn(role="bot", text="Booked.", wrote="create_appointment"),
    ]
    assert check_recovers_gracefully(turns) == []


def test_intent_reversal_silent_write_is_violation() -> None:
    # Caller said "cancel", ASR heard "schedule"; bot books without confirming.
    turns = [
        Turn(
            role="caller",
            text="I want to schedule it",
            garbled=True,
            original="I want to cancel it",
        ),
        Turn(role="bot", text="Sure, booking you in now.", wrote="create_appointment"),
    ]
    violations = check_recovers_gracefully(turns)
    assert len(violations) == 1
    assert violations[0].kind == "plowed_ahead_on_garble"


def test_garble_log_records_by_index() -> None:
    log = GarbleLog()
    log.mark(2, "december fifteenth", "december fifty")
    assert log.by_index[2] == ("december fifteenth", "december fifty")
