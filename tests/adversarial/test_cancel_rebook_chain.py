"""Adversarial: the mid-cancel intent-flip (cancel→rebook) chain.

CLAUDE.md / SOLUTION.md §6: a plain cancel ends the call; only when the caller
flips intent mid-cancel ("actually move it") does a successful
`cancel_appointment` route into BOOK_FLOW (the legacy cancel-then-rebook
chain). This pins both branches so the chain can't silently regress into
either always-ending or always-rebooking.
"""

from __future__ import annotations

from prosper.dispatcher import Dispatcher
from prosper.flows import State
from prosper.result import Ok


class _FakeLLM:
    async def generate(self, **_: object):  # pragma: no cover
        raise AssertionError("LLM must not be called")


class _FakeEHR:
    def set_session_id(self, _s: str) -> None: ...
    def set_turn_id(self, _t: int) -> None: ...


def _confirm_cancel_dispatcher() -> Dispatcher:
    d = Dispatcher(llm=_FakeLLM(), ehr_client=_FakeEHR())  # type: ignore[arg-type]
    d.state = State.CONFIRM_CANCEL
    return d


_CANCEL_OK = Ok(value={"ok": True, "appointment_id": "appt-1"})


def test_plain_cancel_ends_the_call() -> None:
    """No reschedule intent → a successful cancel goes to END."""
    d = _confirm_cancel_dispatcher()
    d.memory.wants_reschedule = False
    d._maybe_transition_from_tool("cancel_appointment", _CANCEL_OK)
    assert d.state is State.END


def test_cancel_with_reschedule_intent_routes_to_book_flow() -> None:
    """Intent flipped mid-cancel → successful cancel routes into BOOK_FLOW so
    the same call can produce the replacement booking."""
    d = _confirm_cancel_dispatcher()
    d.memory.wants_reschedule = True
    d._maybe_transition_from_tool("cancel_appointment", _CANCEL_OK)
    assert d.state is State.BOOK_FLOW
    # The flag must be consumed so a later plain cancel doesn't re-trigger it.
    assert d.memory.wants_reschedule is False


def test_reschedule_intent_set_from_user_text_anywhere() -> None:
    """'move my appointment' said mid-call sets the reschedule flag even from
    a non-CHOOSE_INTENT state (the trigger for the chain above)."""
    d = _confirm_cancel_dispatcher()
    d.memory.wants_reschedule = False
    d._maybe_transition_from_user_text("actually, can you move my appointment to Friday")
    assert d.memory.wants_reschedule is True
