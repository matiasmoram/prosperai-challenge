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


def test_run_all_surfaces_crashed_scenario_without_dropping_siblings() -> None:
    """Async-concurrency regression: a scenario that raises must NOT discard
    the other ``concurrency`` in-flight scenarios. ``asyncio.gather`` without
    ``return_exceptions=True`` would propagate the first exception and let
    the siblings run silently to completion with their results thrown away
    (real OpenAI cost, no signal back to the runner). ``_run_all`` now
    captures every result and converts crashes into failed
    ``ScenarioResult`` entries.
    """
    import asyncio
    from unittest.mock import patch

    from evals.__main__ import _run_all
    from evals.types import Scenario, ScenarioResult, StateExpectation

    good = Scenario(
        name="good",
        tags=frozenset(),
        persona="",
        setup=lambda _s: None,
        expected_state=StateExpectation(),
        judge_criteria=[],
    )
    bad = Scenario(
        name="bad",
        tags=frozenset(),
        persona="",
        setup=lambda _s: None,
        expected_state=StateExpectation(),
        judge_criteria=[],
    )

    async def fake_run(scenario, *, openai_client, mock):  # signature must match run_scenario
        if scenario.name == "bad":
            raise RuntimeError("simulated scenario crash")
        return ScenarioResult(
            name=scenario.name,
            state_pass=True,
            state_reasons=[],
            judge_pass=True,
            judge_justification="ok",
            turns=1,
            duration_ms=1.0,
            transcript=[],
        )

    with patch("evals.__main__.run_scenario", side_effect=fake_run):
        results = asyncio.run(
            _run_all([good, bad], concurrency=2, mock=True)
        )

    by_name = {r.name: r for r in results}
    # Sibling did NOT get cancelled / discarded
    assert by_name["good"].overall_pass is True
    # Crashed sibling surfaces as a failed result, not a raise
    assert by_name["bad"].overall_pass is False
    assert any("simulated scenario crash" in r for r in by_name["bad"].state_reasons)


async def test_dispatcher_exhausted_tool_loop_returns_recovery_line() -> None:
    """Bug-fix regression: when the LLM keeps calling tools every turn for
    all 4 iterations of ``_llm_turn`` without ever returning text, the bot
    used to return ``""`` (silence for the caller). Now it injects a
    recovery line and logs ``llm_loop_exhausted`` so it's visible in
    transcripts / eval output.
    """
    from collections.abc import Iterator

    from prosper.dispatcher import Dispatcher, LLMClientProtocol, LLMReply, ToolCall
    from prosper.flows import State

    class ToolLoopLLM(LLMClientProtocol):
        def __init__(self) -> None:
            # Always returns a tool call, never text. The dispatcher rejects
            # the (out-of-whitelist) tool so state never transitions to END.
            self._calls: Iterator[LLMReply] = iter(
                LLMReply(
                    text="",
                    tool_calls=[ToolCall(name="not_a_real_tool", arguments={})],
                )
                for _ in range(10)
            )

        async def generate(self, *, state, history, tools):  # signature matches LLMClientProtocol
            return next(self._calls)

    # Build a minimal dispatcher; no EHR client needed since the tool call is
    # rejected before reaching it.
    class _StubEHR:
        def set_session_id(self, _s): pass
        def set_turn_id(self, _t): pass

    d = Dispatcher(llm=ToolLoopLLM(), ehr_client=_StubEHR())  # type: ignore[arg-type]
    d.state = State.IDENTIFY_PATIENT
    out = await d.handle_user_turn("hello")
    assert out  # not empty
    assert "having trouble" in out.lower() or "repeat" in out.lower()
    # Marker recorded in transcript so reviewers can spot the loop event
    assert any(e.get("kind") == "llm_loop_exhausted" for e in d.transcript)
