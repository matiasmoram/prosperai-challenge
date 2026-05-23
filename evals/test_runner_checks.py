"""Unit-test the runner's deterministic post-checks (no LLM calls)."""

from evals.mock_llm import MockDispatcherLLM, MockPersonaLLM, available_mock_scenarios
from evals.runner import _check_hallucinated_confirmation, _is_persona_stop
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

    async def fake_run(scenario, *, openai_client, mock, debug=False):
        # signature must match run_scenario (incl. --debug pass-through)
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
        results = asyncio.run(_run_all([good, bad], concurrency=2, mock=True))

    by_name = {r.name: r for r in results}
    # Sibling did NOT get cancelled / discarded
    assert by_name["good"].overall_pass is True
    # Crashed sibling surfaces as a failed result, not a raise
    assert by_name["bad"].overall_pass is False
    assert any("simulated scenario crash" in r for r in by_name["bad"].state_reasons)


def test_persona_stop_existing_phrases_still_trigger() -> None:
    """Regression guard: the pre-existing matches keep working."""
    assert _is_persona_stop("thanks, bye")
    assert _is_persona_stop("Thanks, bye!")
    assert _is_persona_stop("goodbye")
    assert _is_persona_stop("bye!")


def test_persona_stop_new_phrases_trigger_when_standalone() -> None:
    """Each of the new end-of-call phrases triggers when it's the whole
    utterance (case-insensitive, trailing punctuation allowed).
    """
    assert _is_persona_stop("see you")
    assert _is_persona_stop("See you!")
    assert _is_persona_stop("talk to you later")
    assert _is_persona_stop("Talk later.")
    assert _is_persona_stop("never mind")
    assert _is_persona_stop("Nevermind!")
    assert _is_persona_stop("that's all")
    assert _is_persona_stop("That is all.")
    assert _is_persona_stop("I'm done")
    assert _is_persona_stop("im done")
    assert _is_persona_stop("Have a good day")
    assert _is_persona_stop("have a good night!")
    assert _is_persona_stop("have a good one")
    assert _is_persona_stop("cheers")
    assert _is_persona_stop("Take care.")


def test_persona_stop_does_not_trigger_mid_sentence() -> None:
    """Crucial negative cases: stop-words appearing inside a booking request
    must NOT terminate the conversation. The pattern is anchored to the end
    of the utterance so a phrase like "see you next Tuesday" is safe.
    """
    assert not _is_persona_stop("see you next Tuesday at 3pm")
    assert not _is_persona_stop("Can we talk later about pricing? I want to book today.")
    assert not _is_persona_stop("Never mind that, I want to book an appointment")
    assert not _is_persona_stop("Have a good day on Friday is what I need")
    assert not _is_persona_stop("")
    assert not _is_persona_stop("   ")


def test_persona_simulator_honors_prosper_eval_model_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """``PROSPER_EVAL_MODEL`` overrides the default model when the caller
    does not pass an explicit ``model=`` kwarg. Confirms the env-var
    escape hatch added so the judge / persona sim can be swapped without
    a code edit (e.g. to evaluate a different judge model).
    """
    from evals.sim import PersonaSimulator

    monkeypatch.setenv("PROSPER_EVAL_MODEL", "fake-model-xyz")
    sim = PersonaSimulator(client=object(), persona="ignored")
    assert sim._model == "fake-model-xyz"


def test_persona_simulator_falls_back_to_gpt4o_mini_when_env_unset(  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """Default kicks in when ``PROSPER_EVAL_MODEL`` is not set."""
    from evals.sim import PersonaSimulator

    monkeypatch.delenv("PROSPER_EVAL_MODEL", raising=False)
    sim = PersonaSimulator(client=object(), persona="ignored")
    assert sim._model == "gpt-4o-mini"


async def test_judge_transcript_uses_prosper_eval_model_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """When ``PROSPER_EVAL_MODEL`` is set and no ``model=`` kwarg is
    passed, ``judge_transcript`` resolves to the env value at call time
    (not at module-import time, so monkeypatch wins).
    """
    from types import SimpleNamespace

    from evals.judge import judge_transcript

    monkeypatch.setenv("PROSPER_EVAL_MODEL", "fake-judge-model")

    captured: dict[str, str] = {}

    class _StubCompletions:
        async def create(self, **kwargs):  # type: ignore[no-untyped-def]
            captured["model"] = kwargs["model"]
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="PASS - ok"))]
            )

    stub_client = SimpleNamespace(chat=SimpleNamespace(completions=_StubCompletions()))

    is_pass, _ = await judge_transcript(client=stub_client, transcript=[], criteria=["x"])
    assert is_pass
    assert captured["model"] == "fake-judge-model"


async def test_runner_passes_prosper_bot_model_to_openai_adapter(  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """``run_scenario`` constructs ``OpenAILLMAdapter`` with ``PROSPER_BOT_MODEL``
    (mirroring ``bot.py``). Without this, live evals always ran ``gpt-4o-mini``
    regardless of the configured production model.
    """
    import contextlib

    from evals import runner as runner_mod
    from evals.types import Scenario, StateExpectation

    monkeypatch.setenv("PROSPER_BOT_MODEL", "fake-bot-model")

    captured: dict[str, str] = {}

    class _StopRunnerEarly(Exception):
        pass

    class _CaptureAdapter:
        def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
            captured["model"] = kwargs["model"]
            raise _StopRunnerEarly("captured")

    monkeypatch.setattr(runner_mod, "OpenAILLMAdapter", _CaptureAdapter)

    scenario = Scenario(
        name="x",
        tags=frozenset(),
        persona="p",
        setup=lambda _s: None,
        expected_state=StateExpectation(),
        judge_criteria=[],
    )
    with contextlib.suppress(_StopRunnerEarly):
        await runner_mod.run_scenario(scenario, openai_client=object(), mock=False)
    assert captured["model"] == "fake-bot-model"


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
        def set_session_id(self, _s):
            pass

        def set_turn_id(self, _t):
            pass

    d = Dispatcher(llm=ToolLoopLLM(), ehr_client=_StubEHR())  # type: ignore[arg-type]
    d.state = State.IDENTIFY_PATIENT
    out = await d.handle_user_turn("hello")
    assert out  # not empty
    # The exact recovery copy lives in prompts.FALLBACK_LINES; we assert the
    # recovery LINE is the one configured for this failure mode rather than
    # pinning a phrase that drifts whenever the prompt is polished.
    from prosper.prompts import FALLBACK_LINES

    assert out == FALLBACK_LINES["llm_loop_exhausted"]
    # Marker recorded in transcript so reviewers can spot the loop event
    assert any(e.get("kind") == "llm_loop_exhausted" for e in d.transcript)
