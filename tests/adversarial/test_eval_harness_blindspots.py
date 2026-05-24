"""Adversarial (meta): the eval harness can't see the F-009 confirm+goodbye bug.

Finding M-001. `evals.runner.run_scenario` calls `_is_persona_stop(user_text)`
and, on a match, sets `dispatcher.state = State.END` and breaks **before**
`handle_user_turn` runs. So when a persona says "yes, book it, thanks bye" in
one breath, the harness treats it as a clean hang-up and the dispatcher's own
confirm+goodbye routing (F-009 — where the goodbye drops the booking) is never
exercised. The mock and live eval suites therefore cannot catch the F-009
class of bug. (Separately, the suite is hermetic — EHR mounted via
ASGITransport — so the F-008 transport-error class also never fires.)

These plain tests pin the harness behaviour so the blind spot is documented.
"""

from __future__ import annotations

import pytest

from evals.runner import _is_persona_stop


@pytest.mark.parametrize(
    "utterance",
    [
        "yes, book it, thanks bye",
        "yes cancel it, goodbye",
        "yes move it, bye",
    ],
)
def test_combined_confirm_goodbye_is_swallowed_as_a_stop(utterance: str) -> None:
    """A combined confirm+goodbye is classified as an end-of-call stop, so the
    runner forces END and never drives it through the dispatcher — masking
    F-009."""
    assert _is_persona_stop(utterance) is True


@pytest.mark.parametrize(
    "utterance",
    [
        "yes please, that's perfect",
        "okay book the 2pm slot",
        "see you next Tuesday at 3pm",  # mid-sentence 'see you' must NOT stop
    ],
)
def test_non_stop_utterances_reach_the_dispatcher(utterance: str) -> None:
    """Baseline: ordinary confirmations and mid-sentence phrases are NOT stops,
    so they are processed by the dispatcher as normal turns."""
    assert _is_persona_stop(utterance) is False
