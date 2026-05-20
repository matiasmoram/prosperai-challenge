"""Unit-test the runner's deterministic post-checks (no LLM calls)."""

from evals.mock_llm import MockDispatcherLLM, MockPersonaLLM, available_mock_scenarios
from evals.runner import _check_hallucinated_confirmation
from evals.scenarios import SCENARIOS


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


def test_mock_llm_covers_every_scenario() -> None:
    """Sanity-check that ``evals/mock_llm.py`` has a script for every
    ``SCENARIOS`` entry — otherwise ``--mock-llm`` would KeyError on the
    missing one. Cheap regression guard for when new scenarios are added.
    """
    covered = set(available_mock_scenarios())
    missing = {s.name for s in SCENARIOS} - covered
    assert not missing, f"mock LLM missing scripts for: {sorted(missing)}"


def test_mock_dispatcher_llm_constructible_for_every_scenario() -> None:
    """Each script imports & yields without raising. Catches typos in the
    helper functions (e.g. ``_tool("name=", ...)`` collisions) early."""
    for s in SCENARIOS:
        MockDispatcherLLM(s.name)
        MockPersonaLLM(s.name)
