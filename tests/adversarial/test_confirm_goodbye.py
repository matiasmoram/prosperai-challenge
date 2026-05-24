"""Adversarial: a combined "confirm + goodbye" drops the action at CONFIRM_*.

Finding F-009. `_maybe_transition_from_user_text` checks goodbye intent FIRST,
from any state. At a CONFIRM_* state the tool that executes the action
(create/cancel/reschedule) is only reachable while the FSM is still in that
CONFIRM state. A caller who says "yes, book it, thanks bye" in one breath is
routed straight to END — END has no tools — so the booking the caller just
confirmed never happens. The caller's mental model is "I confirmed, so it's
done"; the EHR has nothing. This is the hallucinated-success failure mode.
"""

from __future__ import annotations

import pytest

from prosper.dispatcher import Dispatcher
from prosper.flows import State


class _FakeLLM:
    async def generate(self, **_: object):  # pragma: no cover
        raise AssertionError("LLM must not be called for a pure routing test")


class _FakeEHR:
    def set_session_id(self, _s: str) -> None: ...
    def set_turn_id(self, _t: int) -> None: ...


def _route(state: State, text: str) -> State:
    d = Dispatcher(llm=_FakeLLM(), ehr_client=_FakeEHR())  # type: ignore[arg-type]
    d.state = state
    d._maybe_transition_from_user_text(text)
    return d.state


def test_clean_affirmation_stays_in_confirm_book() -> None:
    """Baseline: a plain 'yes' keeps us in CONFIRM_BOOK so the LLM can book."""
    assert _route(State.CONFIRM_BOOK, "yes book it") is State.CONFIRM_BOOK


@pytest.mark.parametrize(
    "state,text",
    [
        (State.CONFIRM_BOOK, "yes, book it, thanks bye"),
        (State.CONFIRM_CANCEL, "yes cancel it, goodbye"),
        (State.CONFIRM_RESCHEDULE, "yes move it, bye"),
    ],
)
def test_confirm_plus_goodbye_does_not_drop_the_action(state: State, text: str) -> None:
    """F-009 fixed: when an utterance at a CONFIRM_* state BOTH affirms and
    says goodbye, the affirmation wins — the FSM stays in the CONFIRM state so
    the action tool fires; the goodbye is honoured on a later turn."""
    assert _route(state, text) is not State.END
    assert _route(state, text) is state


@pytest.mark.parametrize(
    "state,text",
    [
        (State.CONFIRM_BOOK, "no, cancel that, bye"),
        (State.CONFIRM_CANCEL, "no, goodbye"),
    ],
)
def test_confirm_plus_goodbye_without_affirmation_still_ends(state: State, text: str) -> None:
    """A goodbye with NO affirmation at a CONFIRM state still ends the call —
    the F-009 carve-out is gated on an affirmation being present."""
    assert _route(state, text) is State.END
