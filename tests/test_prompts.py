"""Sanity checks on prompt sizes — persona large enough for OpenAI cache,
per-state task messages tight enough to stay fast."""

from prosper.prompts import (
    CLINIC_PERSONA,
    MIN_PERSONA_TOKENS_FOR_CACHE,
    TASK_MESSAGES,
)


def _approx_tokens(s: str) -> int:
    return len(s) // 4


def test_persona_long_enough_for_prompt_cache() -> None:
    assert _approx_tokens(CLINIC_PERSONA) >= MIN_PERSONA_TOKENS_FOR_CACHE


def test_persona_mentions_core_responsibilities() -> None:
    lower = CLINIC_PERSONA.lower()
    for phrase in ("prosper health", "appointment", "cancel", "confirm"):
        assert phrase in lower, f"missing '{phrase}' in persona"


def test_every_state_has_a_task_message() -> None:
    expected = {
        "GREETING",
        "IDENTIFY_PATIENT",
        "REGISTER_PATIENT",
        "CHOOSE_INTENT",
        "BOOK_FLOW",
        "CANCEL_FLOW",
        "CONFIRM_BOOK",
        "CONFIRM_CANCEL",
        "END",
    }
    assert set(TASK_MESSAGES) == expected


def test_each_task_message_under_1_kb() -> None:
    for state, msg in TASK_MESSAGES.items():
        assert len(msg) < 1024, f"{state} task message is {len(msg)} bytes (>=1024)"
