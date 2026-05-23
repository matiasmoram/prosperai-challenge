"""Adversarial: CHOOSE_INTENT misroutes booking phrases that contain a
cancel-ish filler verb to CANCEL_FLOW.

Finding F-006. `_maybe_transition_from_user_text` checks `_CANCEL_INTENT`
(broadened to skip|drop|move|remove|delete|…) BEFORE `_BOOK_INTENT`. A caller
who clearly wants to BOOK but phrases it with one of those common verbs
("let's skip the chit-chat, I want to book") is routed to CANCEL_FLOW. For a
new patient that dead-ends the call ("nothing to cancel" → END); for an
existing one the bot starts reading appointments to cancel.
"""

from __future__ import annotations

import pytest

from prosper.dispatcher import Dispatcher
from prosper.flows import State


class _FakeLLM:
    async def generate(self, **_: object):  # pragma: no cover - never called
        raise AssertionError("LLM must not be called for a pure routing test")


class _FakeEHR:
    def set_session_id(self, _s: str) -> None: ...
    def set_turn_id(self, _t: int) -> None: ...


def _route_from_choose_intent(text: str) -> State:
    d = Dispatcher(llm=_FakeLLM(), ehr_client=_FakeEHR())  # type: ignore[arg-type]
    d.state = State.CHOOSE_INTENT
    d._maybe_transition_from_user_text(text)
    return d.state


@pytest.mark.parametrize(
    "text",
    [
        "let's skip the chit-chat, I want to book an appointment",
        "I want to move forward with booking a visit",
        "remove all this confusion and just schedule me",
    ],
)
@pytest.mark.xfail(
    strict=True,
    reason="F-006: a phrase with an explicit booking word (book/booking/"
    "schedule) must route to BOOK_FLOW even when it also contains a "
    "cancel-ish filler verb (skip/move/remove). Cancel intent is checked "
    "first and wins.",
)
def test_explicit_booking_phrase_routes_to_book_flow(text: str) -> None:
    assert _route_from_choose_intent(text) is State.BOOK_FLOW


def test_plain_book_phrase_routes_to_book_flow() -> None:
    """Baseline: an unambiguous booking phrase routes correctly."""
    assert _route_from_choose_intent("can you book me a new appointment") is State.BOOK_FLOW


def test_plain_cancel_phrase_routes_to_cancel_flow() -> None:
    assert _route_from_choose_intent("I need to cancel my appointment") is State.CANCEL_FLOW


def test_reschedule_phrase_routes_to_reschedule_flow() -> None:
    assert _route_from_choose_intent("I want to reschedule my appointment") is State.RESCHEDULE_FLOW
