"""Adversarial: a medical-emergency red flag is now FSM-enforced (F-011 fixed).

`suggest_specialty` returns `Err(code="medical_emergency")` when the triage
mini-LLM sets `red_flag` (chest pain, suicidal ideation, …).
`Dispatcher._maybe_transition_from_tool` now routes that Err from BOOK_FLOW to
END via the `medical_emergency` edge, so the booking tools are physically
unmounted — the agent cannot book a routine visit for a caller in a medical
emergency even if the LLM ignores the persona's 911-redirect guidance. These
tests pin the hard guardrail.
"""

from __future__ import annotations

from prosper.dispatcher import Dispatcher
from prosper.flows import ALLOWED_TOOLS, State
from prosper.result import Err


class _FakeLLM:
    async def generate(self, **_: object):  # pragma: no cover
        raise AssertionError("LLM must not be called for a pure FSM test")


class _FakeEHR:
    def set_session_id(self, _s: str) -> None: ...
    def set_turn_id(self, _t: int) -> None: ...


def _dispatcher_in_book_flow() -> Dispatcher:
    d = Dispatcher(llm=_FakeLLM(), ehr_client=_FakeEHR())  # type: ignore[arg-type]
    d.state = State.BOOK_FLOW
    return d


def test_medical_emergency_err_forces_transition_to_end() -> None:
    """F-011 fixed: a medical_emergency Err from suggest_specialty routes the
    FSM to END so the booking path is unmounted."""
    d = _dispatcher_in_book_flow()
    err = Err(code="medical_emergency", message="possible emergency", retryable=False)
    d._maybe_transition_from_tool("suggest_specialty", err)
    assert d.state is State.END


def test_booking_tools_unmounted_after_emergency_flag() -> None:
    """After the emergency transition the FSM is at END, which has no tools —
    the agent physically cannot book, independent of the LLM (F-011 fixed)."""
    d = _dispatcher_in_book_flow()
    err = Err(code="medical_emergency", message="possible emergency", retryable=False)
    d._maybe_transition_from_tool("suggest_specialty", err)
    assert ALLOWED_TOOLS[d.state] == set()
    assert "list_availability_slots" not in ALLOWED_TOOLS[d.state]


def test_non_emergency_specialty_does_not_change_state() -> None:
    """A normal (Ok) suggest_specialty result keeps the call in BOOK_FLOW."""
    from prosper.result import Ok

    d = _dispatcher_in_book_flow()
    ok = Ok(value={"specialty": "Therapist", "duration_minutes": 60})
    d._maybe_transition_from_tool("suggest_specialty", ok)
    assert d.state is State.BOOK_FLOW
