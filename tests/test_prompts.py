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
        "RESCHEDULE_FLOW",
        "CONFIRM_BOOK",
        "CONFIRM_CANCEL",
        "CONFIRM_RESCHEDULE",
        "END",
    }
    assert set(TASK_MESSAGES) == expected


def test_each_task_message_under_1_kb() -> None:
    for state, msg in TASK_MESSAGES.items():
        assert len(msg) < 1024, f"{state} task message is {len(msg)} bytes (>=1024)"


def test_persona_has_adversarial_refusal_patterns() -> None:
    """Persona must carry canned refusals so the bot doesn't improvise."""
    lower = CLINIC_PERSONA.lower()
    # Off-topic / out-of-scope front-desk redirect.
    assert "front desk" in lower
    # Cross-patient refusal.
    assert "your own appointments" in lower
    # Generic decline.
    assert "i can't do that" in lower or "i cannot do that" in lower


def test_persona_forbids_hallucinated_success_and_injection() -> None:
    """Persona must explicitly cover the adversarial-eval attack surface."""
    lower = CLINIC_PERSONA.lower()
    # No success without a tool call.
    assert "returned an ok" in lower or "returned ok" in lower
    # Treat field contents as literal data, not instructions.
    assert "literal" in lower and "instruction" in lower
    # No leaking ids / internal state.
    assert "slot id" in lower or "slot ids" in lower
    # No quoting the persona back.
    assert "quote" in lower or "paraphrase" in lower


def test_register_patient_has_literal_name_defense() -> None:
    msg = TASK_MESSAGES["REGISTER_PATIENT"].lower()
    assert "literal name" in msg or "not an instruction" in msg


def test_greeting_opens_with_branded_name_first_line() -> None:
    """The opening line must brand the clinic and ask the caller's name + intent
    in a single sentence, so the dispatcher can launch identity lookup as
    soon as the caller answers (Subproblem B, 2026-05-23)."""
    msg = TASK_MESSAGES["GREETING"]
    assert "Prosper Health" in msg
    assert "what's your name" in msg
    assert "how can I help you today" in msg


def test_identify_patient_handles_name_already_known() -> None:
    """IDENTIFY_PATIENT must read the name from history when the greeting
    already collected it, rather than asking for phone first every time."""
    msg = TASK_MESSAGES["IDENTIFY_PATIENT"].lower()
    assert "name known" in msg or "read it from history" in msg
    assert "find_patient_by_name_dob" in msg


def test_identify_patient_disambiguates_multiple_matches() -> None:
    """IDENTIFY_PATIENT must instruct the LLM to ask which patient when more
    than one matches, instead of guessing (Subproblem C, 2026-05-23)."""
    msg = TASK_MESSAGES["IDENTIFY_PATIENT"].lower()
    assert "more than one" in msg
    assert "which one" in msg
