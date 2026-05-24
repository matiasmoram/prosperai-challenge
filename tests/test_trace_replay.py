"""Golden-trace replay regression guard (FUTURE.md 2.3).

Each golden trace in ``evals/traces/<name>.json`` pins the exact ordered
sequence of FSM transitions + tool outcomes for a scenario. This test replays
every golden through the mock runner and asserts the fingerprint matches —
catching transition reorderings, dropped tool calls, or spurious rejections
that the delta-only mock-eval checks would miss. Zero tokens, no API key.

Regenerate goldens after an intentional flow change:
    uv run python -m evals.trace_replay --record
"""

from __future__ import annotations

import pytest

from evals.trace_replay import GOLDEN_SCENARIOS, fingerprint_for, load_golden


@pytest.mark.parametrize("name", GOLDEN_SCENARIOS)
async def test_golden_trace_matches(name: str) -> None:
    golden = load_golden(name)
    actual = await fingerprint_for(name)
    assert actual == golden, (
        f"{name}: transition/tool fingerprint drifted from golden. "
        f"If this change is intentional, re-record with "
        f"`uv run python -m evals.trace_replay --record`.\n"
        f"golden={golden}\nactual={actual}"
    )


def test_golden_files_exist_for_every_listed_scenario() -> None:
    """Every name in GOLDEN_SCENARIOS must have a committed golden file."""
    for name in GOLDEN_SCENARIOS:
        # load_golden raises FileNotFoundError if the golden is missing.
        assert load_golden(name), f"{name}: golden fingerprint is empty"
