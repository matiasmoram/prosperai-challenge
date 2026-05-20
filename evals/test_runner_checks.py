"""Unit-test the runner's deterministic post-checks (no LLM calls)."""

from evals.runner import _check_hallucinated_confirmation


def test_flags_claim_without_matching_tool_ok() -> None:
    transcript = [
        {"kind": "user", "text": "cancel it"},
        {"kind": "assistant", "state": "CANCEL_FLOW", "text": "I've cancelled that for you."},
    ]
    reasons = _check_hallucinated_confirmation(transcript)
    assert len(reasons) == 1
    assert "hallucinated confirmation" in reasons[0]


def test_accepts_claim_when_tool_ok_in_window() -> None:
    transcript = [
        {"kind": "user", "text": "yes"},
        {"kind": "tool_ok", "name": "cancel_appointment", "value": {"ok": True}},
        {
            "kind": "assistant",
            "state": "END",
            "text": "Done — your appointment has been cancelled.",
        },
    ]
    assert _check_hallucinated_confirmation(transcript) == []


def test_ignores_neutral_assistant_turns() -> None:
    transcript = [
        {"kind": "assistant", "state": "GREETING", "text": "Hi! Book or cancel today?"},
        {"kind": "user", "text": "book please"},
        {"kind": "assistant", "state": "IDENTIFY_PATIENT", "text": "What's your phone number?"},
    ]
    assert _check_hallucinated_confirmation(transcript) == []
