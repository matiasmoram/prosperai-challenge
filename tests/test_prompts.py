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
