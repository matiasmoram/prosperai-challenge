"""Adversarial: a mid-utterance "bye" hangs up the call (STT homophone trap).

Finding F-012. `_GOODBYE_HARD` matches `\bbye\b` ANYWHERE in the utterance.
ElevenLabs realtime STT readily mishears the very common "by the way" as
"bye the way" — which then routes the caller straight to END mid-task,
abandoning an in-progress booking they were about to complete.
"""

from __future__ import annotations

import pytest

from prosper.dispatcher import Dispatcher, _has_goodbye_intent
from prosper.flows import State


class _FakeLLM:
    async def generate(self, **_: object):  # pragma: no cover
        raise AssertionError("LLM must not be called")


class _FakeEHR:
    def set_session_id(self, _s: str) -> None: ...
    def set_turn_id(self, _t: int) -> None: ...


def _route(state: State, text: str) -> State:
    d = Dispatcher(llm=_FakeLLM(), ehr_client=_FakeEHR())  # type: ignore[arg-type]
    d.state = state
    d._maybe_transition_from_user_text(text)
    return d.state


def test_real_goodbye_still_ends_the_call() -> None:
    """Baseline: a genuine sign-off must still route to END."""
    assert _route(State.BOOK_FLOW, "okay, goodbye") is State.END
    assert _route(State.BOOK_FLOW, "thanks, bye") is State.END


def test_standby_and_by_the_way_do_not_hang_up() -> None:
    """Controls: 'by the way' and 'standby' contain 'by', not 'bye' — no stop."""
    assert _route(State.BOOK_FLOW, "by the way, can you book me Tuesday") is State.BOOK_FLOW
    assert _route(State.BOOK_FLOW, "my standby appointment — book Tuesday") is State.BOOK_FLOW


@pytest.mark.parametrize(
    "text",
    [
        "bye the way, can you also book me Tuesday",
        "the kids said bye to grandma, anyway book Tuesday",
    ],
)
def test_midutterance_bye_does_not_hang_up(text: str) -> None:
    """F-012 fixed: a 'bye' embedded mid-utterance (the STT homophone of 'by
    the way') no longer hangs up an in-progress task."""
    assert _has_goodbye_intent(text) is False
    assert _route(State.BOOK_FLOW, text) is not State.END


def test_trailing_bye_still_ends() -> None:
    """A bare 'bye' at the end of the utterance is still a sign-off."""
    assert _has_goodbye_intent("ok, bye") is True
    assert _route(State.BOOK_FLOW, "ok, bye") is State.END
