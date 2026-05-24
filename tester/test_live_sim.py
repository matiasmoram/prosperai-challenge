"""Live smoke test for the autonomous simulator.

Gated on ``PROSPER_EVAL_LIVE=1`` (+ a key) exactly like ``make eval`` — so
``make verify`` never spends tokens on it. Runs the single highest-signal
adversarial persona (``hallucination_bait``) and asserts the bot stayed honest:
no unbacked outcome, no spoken hallucinated confirmation.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

load_dotenv()

_LIVE = bool(os.environ.get("PROSPER_EVAL_LIVE")) and bool(os.environ.get("OPENAI_API_KEY"))


@pytest.mark.live
@pytest.mark.skipif(not _LIVE, reason="set PROSPER_EVAL_LIVE=1 (+OPENAI_API_KEY) to run live sim")
async def test_hallucination_bait_stays_honest() -> None:
    from openai import AsyncOpenAI

    from tester.invariants import check_call
    from tester.live_sim import simulate_call
    from tester.personas import CURATED

    persona = next(p for p in CURATED if p.name == "hallucination_bait")
    result = await simulate_call(persona, client=AsyncOpenAI())
    assert result.error is None, result.error
    violations = check_call(result.events, result.transcript)
    assert not violations, "; ".join(str(v) for v in violations)
