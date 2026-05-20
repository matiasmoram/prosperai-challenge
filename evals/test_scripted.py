"""Pytest wrapper that runs every scenario as a test. Requires OPENAI_API_KEY.

Tests are skipped (not failed) when the env var is missing, so external forks
and CI without secrets get a clean lint/type/unit pass.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — skipping scenario evals",
)


@pytest.mark.parametrize("scenario", [], ids=lambda s: s.name)
async def test_placeholder(scenario) -> None:  # pragma: no cover
    """Placeholder. Real parametrisation happens at collection time below."""
    pass


def pytest_generate_tests(metafunc) -> None:  # noqa: D401
    """Collect scenarios lazily so importing the module without OpenAI
    works for plain ``pytest --collect-only``.
    """
    if "scenario" in metafunc.fixturenames and metafunc.function.__name__ == "test_scenario":
        from evals.scenarios import SCENARIOS

        metafunc.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)


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
