"""Pytest wrapper that runs every scenario as a test.

Two flavours:

* ``test_scenario`` — the real eval, runs against OpenAI. Requires
  ``OPENAI_API_KEY``; SKIPPED on a clean checkout / CI without secrets.
* ``test_scenario_mock`` — runs the same 16 scenarios through the
  deterministic mock LLM in ``evals/mock_llm.py``. No API key needed,
  each scenario finishes in <100ms. The state-delta assertion is the only
  signal that matters here; the judge step is mocked to always-PASS.

The mock variant lets external forks and CI exercise the runner harness
(transcript collection, SQLite isolation, hallucination regex, state
deltas) end-to-end without paying for tokens.
"""

from __future__ import annotations

import os

import pytest

# Live evals are opt-in via PROSPER_EVAL_LIVE=1 to avoid:
# (a) silent skips when OPENAI_API_KEY is present but the account has no
#     quota (`insufficient_quota` 429), which would otherwise leave the
#     real-LLM test fail-flapping every local run, and
# (b) accidental spend during a routine `make verify` / pre-commit.
# Set PROSPER_EVAL_LIVE=1 alongside a funded OPENAI_API_KEY to opt in.
_NEEDS_KEY = pytest.mark.skipif(
    not (os.environ.get("OPENAI_API_KEY") and os.environ.get("PROSPER_EVAL_LIVE") == "1"),
    reason="set PROSPER_EVAL_LIVE=1 + OPENAI_API_KEY to run live scenario evals",
)


@pytest.mark.parametrize("scenario", [], ids=lambda s: s.name)
async def test_placeholder(scenario) -> None:  # pragma: no cover
    """Placeholder. Real parametrisation happens at collection time below."""
    pass


def pytest_generate_tests(metafunc) -> None:
    """Collect scenarios lazily so importing the module without OpenAI
    works for plain ``pytest --collect-only``.
    """
    if "scenario" in metafunc.fixturenames and metafunc.function.__name__ in (
        "test_scenario",
        "test_scenario_mock",
    ):
        from evals.scenarios import SCENARIOS

        metafunc.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)


@_NEEDS_KEY
async def test_scenario(scenario) -> None:  # type: ignore[no-redef]
    from openai import AsyncOpenAI

    from evals.runner import run_scenario

    client = AsyncOpenAI()
    result = await run_scenario(scenario, openai_client=client)
    if not result.overall_pass:
        msg = (
            f"\nSTATE checks: {'PASS' if result.state_pass else 'FAIL'} — {result.state_reasons}"
            f"\nJUDGE: {'PASS' if result.judge_pass else 'FAIL'} — {result.judge_justification}"
            f"\nTurns: {result.turns}  Duration: {result.duration_ms:.0f}ms"
        )
        pytest.fail(msg)


# Scenarios whose canned mock script does not perfectly reach the expected
# state-delta (acknowledged limitation — see evals/mock_llm.py). These are
# xfailed under the mock harness so we still exercise the *runner* for them
# without flooding pytest with failures. Drop a name from this set once its
# mock script is tightened.
_MOCK_XFAIL: frozenset[str] = frozenset()


async def test_scenario_mock(scenario) -> None:  # type: ignore[no-redef]
    """Run every scenario through the deterministic mock LLM."""
    from evals.runner import run_scenario

    result = await run_scenario(scenario, mock=True)
    if scenario.name in _MOCK_XFAIL and not result.overall_pass:
        pytest.xfail(f"mock script known-incomplete for {scenario.name}: {result.state_reasons}")
    if not result.overall_pass:
        msg = (
            f"\nSTATE checks: {'PASS' if result.state_pass else 'FAIL'} — {result.state_reasons}"
            f"\nJUDGE: {'PASS' if result.judge_pass else 'FAIL'} — {result.judge_justification}"
            f"\nTurns: {result.turns}  Duration: {result.duration_ms:.0f}ms"
            f"\nTranscript (last 8 events): {result.transcript[-8:]}"
        )
        pytest.fail(msg)
