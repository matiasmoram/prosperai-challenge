"""Adversarial: handle "0" must never resolve to the last slot (negative index).

`_maybe_handle_index("0")` returns -1 (1-based → 0-based off-by-one). The
resolution call sites guard with `0 <= idx < len(...)`, so a handle of "0" is
left unresolved and then rejected as a hallucinated id. This pins that safety
property: drop the lower-bound check in a refactor and "slot 0" would silently
book `last_slots[-1]` — the WRONG slot. (Fuzzing 30k random handles found no
crashes; "0"/"#0" are the only inputs that yield a negative index.)
"""

from __future__ import annotations

from prosper.dispatcher import Dispatcher, _maybe_handle_index
from prosper.flows import State


class _FakeLLM:
    async def generate(self, **_: object):  # pragma: no cover
        raise AssertionError("LLM must not be called")


class _FakeEHR:
    def set_session_id(self, _s: str) -> None: ...
    def set_turn_id(self, _t: int) -> None: ...


def _dispatcher_with_slots() -> Dispatcher:
    d = Dispatcher(llm=_FakeLLM(), ehr_client=_FakeEHR())  # type: ignore[arg-type]
    d.state = State.CONFIRM_BOOK
    d.memory.identified_patient = {"id": "p1"}
    d.memory.last_slots = [{"slot_id": "uuid-A"}, {"slot_id": "uuid-B"}]
    return d


def test_maybe_handle_index_zero_is_negative_one() -> None:
    """Documents the off-by-one: handle '0' maps to index -1."""
    assert _maybe_handle_index("0") == -1
    assert _maybe_handle_index("#0") == -1


def test_handle_zero_does_not_resolve_to_last_slot() -> None:
    """A slot_id of '0' must stay unresolved (NOT become last_slots[-1])."""
    d = _dispatcher_with_slots()
    args = {"patient_id": "p1", "slot_id": "0"}
    d._resolve_memory_handles("create_appointment", args)
    assert args["slot_id"] == "0", "handle '0' must not negative-index into last_slots"
    err = d._validate_against_memory("create_appointment", args)
    assert err is not None and err.code == "hallucinated_slot_id"


def test_handle_one_resolves_to_first_slot() -> None:
    """Baseline: a valid handle '1' resolves to the first offered slot."""
    d = _dispatcher_with_slots()
    args = {"patient_id": "p1", "slot_id": "1"}
    d._resolve_memory_handles("create_appointment", args)
    assert args["slot_id"] == "uuid-A"
    assert d._validate_against_memory("create_appointment", args) is None


def test_handle_out_of_range_does_not_resolve() -> None:
    """A handle past the end ('9') stays unresolved → hallucinated, not a crash."""
    d = _dispatcher_with_slots()
    args = {"patient_id": "p1", "slot_id": "9"}
    d._resolve_memory_handles("create_appointment", args)
    assert args["slot_id"] == "9"
    err = d._validate_against_memory("create_appointment", args)
    assert err is not None and err.code == "hallucinated_slot_id"
