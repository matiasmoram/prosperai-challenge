"""Coverage-gap tests for ``prosper.dispatcher`` — surgical, fast, no LLM/no network.

Targets the branches that the happy-path tests in ``test_dispatcher.py`` don't
exercise: every ``_redact_for_llm`` branch, the Err recording path, the
``patient_id_mismatch`` guard, hallucinated-appointment guard, every
TRANSITIONS edge (including ``abort`` rollback edges out of CONFIRM_*), the
sliding-window history cap, and the LLM-usage accumulator.

Uses the shared ``ehr_client`` fixture (see ``tests/conftest.py``).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from prosper.dispatcher import (
    Dispatcher,
    LLMClientProtocol,
    LLMReply,
    LLMUsage,
    SessionMemory,
    ToolCall,
    _redact_for_llm,
)
from prosper.ehr_client import EHRClient
from prosper.flows import State
from prosper.result import Err, Ok


class CannedLLM(LLMClientProtocol):
    def __init__(self, replies: list[LLMReply]) -> None:
        self._iter: Iterator[LLMReply] = iter(replies)
        self.received_states: list[str] = []

    async def generate(self, *, state: str, history: list[dict], tools: list[dict]) -> LLMReply:
        self.received_states.append(state)
        try:
            return next(self._iter)
        except StopIteration:
            return LLMReply(text="", tool_calls=[])


# ---------------------------------------------------------------------------
# _redact_for_llm — every branch
# ---------------------------------------------------------------------------


def test_redact_list_availability_slots_with_slots() -> None:
    out = _redact_for_llm(
        "list_availability_slots",
        {
            "total_returned": 2,
            "slots": [
                {"start_at_iso": "2026-05-21T10:00", "provider_name": "Dr. Patel"},
                {"start_at_iso": "2026-05-21T10:30", "provider_name": "Dr. Patel"},
            ],
        },
    )
    # The redacted line now leads with the total count so the LLM can apply
    # the adaptive offer rule (many → invert; few → read 2-3).
    assert "2 slots available" in out
    assert "Dr. Patel" in out
    # Slot ids must NOT leak into what the LLM sees.
    assert "slot_id" not in out


def test_redact_list_availability_slots_empty() -> None:
    # With no auto-scan match the empty-result line now mentions the
    # 6-day look-ahead so the LLM doesn't repeat the same query.
    assert _redact_for_llm("list_availability_slots", {"slots": []}) == (
        "no slots available for that date or the next 6 days"
    )


def test_redact_list_availability_slots_empty_with_fallback() -> None:
    # When the asked date is empty but the auto-scan finds slots on a
    # later date, _redact_for_llm surfaces the fallback so the LLM can
    # offer it to the caller without making a second tool call.
    out = _redact_for_llm(
        "list_availability_slots",
        {
            "asked_date": "2026-05-27",
            "slots": [],
            "next_day_with_slots": {
                "date": "2026-05-28",
                "slots": [
                    {
                        "slot_id": "abc",
                        "start_at_iso": "2026-05-28T10:00",
                        "end_at_iso": "2026-05-28T10:30",
                        "provider_id": "p",
                        "provider_name": "Dr. Patel",
                    },
                ],
            },
        },
    )
    assert "no slots on 2026-05-27" in out
    assert "next available is 2026-05-28" in out
    assert "Dr. Patel" in out
    assert "slot_id" not in out


def test_redact_upcoming_appointments_with_items() -> None:
    out = _redact_for_llm(
        "get_upcoming_appointments",
        {
            "appointments": [
                {"start_at": "2026-05-21T10:00", "provider_name": "Dr. Patel"},
            ]
        },
    )
    assert out.startswith("upcoming: [1] 2026-05-21T10:00")


def test_redact_upcoming_appointments_empty() -> None:
    assert (
        _redact_for_llm("get_upcoming_appointments", {"appointments": []})
        == "no upcoming appointments"
    )


def test_redact_find_patient_by_phone_with_matches() -> None:
    out = _redact_for_llm(
        "find_patient_by_phone",
        {"patients": [{"first_name": "Ada", "last_name": "Lovelace", "dob": "1990-12-10"}]},
    )
    assert "Ada Lovelace" in out
    assert "DOB 1990-12-10" in out


def test_redact_find_patient_by_phone_empty() -> None:
    assert _redact_for_llm("find_patient_by_phone", {"patients": []}) == "no patient found"


def test_redact_find_patient_by_name_dob_empty() -> None:
    # Same branch as by_phone — keeps the "no patient found" string stable
    assert _redact_for_llm("find_patient_by_name_dob", {"patients": []}) == "no patient found"


def test_redact_create_patient() -> None:
    out = _redact_for_llm(
        "create_patient",
        {"first_name": "Ada", "last_name": "Lovelace", "phone": "+12025550100"},
    )
    assert "patient registered" in out
    assert "Ada Lovelace" in out


def test_redact_create_appointment() -> None:
    out = _redact_for_llm(
        "create_appointment",
        {"start_at": "2026-05-21T10:00", "provider_name": "Dr. Patel"},
    )
    assert "booked" in out
    assert "Dr. Patel" in out


def test_redact_cancel_appointment() -> None:
    assert _redact_for_llm("cancel_appointment", {}) == "appointment cancelled"


def test_redact_unknown_tool_returns_ok() -> None:
    assert _redact_for_llm("unknown_tool_name", {}) == "ok"


# ---------------------------------------------------------------------------
# _validate_against_memory — both Err branches
# ---------------------------------------------------------------------------


def test_validate_against_memory_patient_id_mismatch(ehr_client: EHRClient) -> None:
    """create_appointment with a patient_id != the identified patient must Err."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.memory = SessionMemory(
        identified_patient={"id": "patient-A"},
        last_slots=[{"slot_id": "slot-1"}],
    )
    err = d._validate_against_memory(
        "create_appointment", {"slot_id": "slot-1", "patient_id": "patient-B"}
    )
    assert err is not None
    assert err.code == "patient_id_mismatch"
    assert err.retryable is False


def test_resolve_handles_overrides_bogus_patient_id_for_get_upcoming(
    ehr_client: EHRClient,
) -> None:
    """The LLM never has the real patient UUID (it's redacted), so it passes the
    caller's NAME as patient_id. The dispatcher must OVERRIDE it with the
    identified patient's real id — not just fill when absent. Filling-only-when-
    absent let "Ada Lovelace" reach the EHR, which 404'd and silently broke the
    whole cancel/reschedule flow (appointment list never loaded → FSM never
    reached CONFIRM_CANCEL → cancel_appointment never mounted)."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.memory = SessionMemory(identified_patient={"id": "patient-real-uuid"})
    args = {"patient_id": "Ada Lovelace"}  # name, as the LLM actually sends
    d._resolve_memory_handles("get_upcoming_appointments", args)
    assert args["patient_id"] == "patient-real-uuid"


def test_resolve_handles_overrides_bogus_patient_id_for_create_appointment(
    ehr_client: EHRClient,
) -> None:
    """Same override applies to create_appointment so a name-as-patient_id can't
    slip past and bounce as patient_id_mismatch / 404."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.memory = SessionMemory(
        identified_patient={"id": "patient-real-uuid"},
        last_slots=[{"slot_id": "slot-1"}],
    )
    args = {"slot_id": "1", "patient_id": "Ada Lovelace"}
    d._resolve_memory_handles("create_appointment", args)
    assert args["patient_id"] == "patient-real-uuid"


def test_validate_against_memory_hallucinated_appointment_id(ehr_client: EHRClient) -> None:
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.memory = SessionMemory(last_upcoming_appointments=[{"id": "real-appt"}])
    err = d._validate_against_memory("cancel_appointment", {"appointment_id": "fake-appt"})
    assert err is not None
    assert err.code == "hallucinated_appointment_id"
    assert err.retryable is True


def test_validate_against_memory_allows_known_appointment_id(ehr_client: EHRClient) -> None:
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.memory = SessionMemory(last_upcoming_appointments=[{"id": "real-appt"}])
    assert d._validate_against_memory("cancel_appointment", {"appointment_id": "real-appt"}) is None


def test_validate_against_memory_skips_for_read_tools(ehr_client: EHRClient) -> None:
    """Read-only tools aren't guarded — only writes are."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    assert d._validate_against_memory("find_patient_by_phone", {"phone": "anything"}) is None


# Mutation-survivor regressions: the ``X is not None and X not in known`` guard
# in _validate_against_memory survives an inversion to ``X is None or X not in
# known`` if no test exercises the case where the id is absent from args. Pin
# both halves of the AND.


def test_validate_against_memory_rejects_missing_slot_id(ehr_client: EHRClient) -> None:
    """A create_appointment call without slot_id is a structured Err (not a
    handler TypeError). Live eval surfaced this — the LLM occasionally omits
    a required field and the dispatcher must return a retryable Err pointing
    the model at the missing piece."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.memory = SessionMemory(last_slots=[{"slot_id": "slot-1"}])
    err = d._validate_against_memory("create_appointment", {"patient_id": "p1"})
    assert err is not None
    assert err.code == "missing_slot_id"
    assert err.retryable is True


def test_validate_against_memory_rejects_missing_appointment_id(ehr_client: EHRClient) -> None:
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.memory = SessionMemory(last_upcoming_appointments=[{"id": "real-appt"}])
    err = d._validate_against_memory("cancel_appointment", {"reason": "test"})
    assert err is not None
    assert err.code == "missing_appointment_id"
    assert err.retryable is True


def test_validate_against_memory_allows_known_slot_id(ehr_client: EHRClient) -> None:
    """Pin the inclusive 'known slot' branch — the guard must NOT Err when
    slot_id is in last_slots, no matter how the boolean is flipped."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.memory = SessionMemory(
        identified_patient={"id": "patient-A"},
        last_slots=[{"slot_id": "slot-known"}],
    )
    err = d._validate_against_memory(
        "create_appointment", {"slot_id": "slot-known", "patient_id": "patient-A"}
    )
    assert err is None


# ---------------------------------------------------------------------------
# F-002: clinical floor guard in _validate_against_memory
# ---------------------------------------------------------------------------


def test_validate_against_memory_below_minimum_safe_duration(ehr_client: EHRClient) -> None:
    """F-002: create_appointment with duration_minutes < minimum_safe_minutes must Err."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.memory = SessionMemory(
        identified_patient={"id": "patient-A"},
        last_slots=[{"slot_id": "slot-1"}],
        minimum_safe_minutes=60,
    )
    err = d._validate_against_memory(
        "create_appointment",
        {"slot_id": "slot-1", "patient_id": "patient-A", "duration_minutes": 30},
    )
    assert err is not None
    assert err.code == "below_minimum_safe_duration"
    assert err.retryable is True


def test_validate_against_memory_at_floor_passes(ehr_client: EHRClient) -> None:
    """F-002: duration_minutes == minimum_safe_minutes must pass (floor is inclusive)."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.memory = SessionMemory(
        identified_patient={"id": "patient-A"},
        last_slots=[{"slot_id": "slot-1"}],
        minimum_safe_minutes=60,
    )
    err = d._validate_against_memory(
        "create_appointment",
        {"slot_id": "slot-1", "patient_id": "patient-A", "duration_minutes": 60},
    )
    assert err is None


def test_validate_against_memory_no_triage_skips_floor_check(ehr_client: EHRClient) -> None:
    """F-002: when minimum_safe_minutes is None (no triage ran), the floor guard
    must not fire even for a 30-min booking — callers who bypass triage are unconstrained."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.memory = SessionMemory(
        identified_patient={"id": "patient-A"},
        last_slots=[{"slot_id": "slot-1"}],
        minimum_safe_minutes=None,
    )
    err = d._validate_against_memory(
        "create_appointment",
        {"slot_id": "slot-1", "patient_id": "patient-A", "duration_minutes": 30},
    )
    assert err is None


def test_validate_against_memory_above_floor_passes(ehr_client: EHRClient) -> None:
    """F-002: duration_minutes > minimum_safe_minutes must pass."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.memory = SessionMemory(
        identified_patient={"id": "patient-A"},
        last_slots=[{"slot_id": "slot-1"}],
        minimum_safe_minutes=60,
    )
    err = d._validate_against_memory(
        "create_appointment",
        {"slot_id": "slot-1", "patient_id": "patient-A", "duration_minutes": 90},
    )
    assert err is None


def test_validate_against_memory_below_floor_as_string_still_rejected(
    ehr_client: EHRClient,
) -> None:
    """F-002 regression: the LLM may emit duration_minutes as a string ("30").
    Pydantic on the EHR coerces it, so the floor guard must coerce identically —
    otherwise it fails open and a sub-floor booking slips past."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.memory = SessionMemory(
        identified_patient={"id": "patient-A"},
        last_slots=[{"slot_id": "slot-1"}],
        minimum_safe_minutes=60,
    )
    err = d._validate_against_memory(
        "create_appointment",
        {"slot_id": "slot-1", "patient_id": "patient-A", "duration_minutes": "30"},
    )
    assert err is not None
    assert err.code == "below_minimum_safe_duration"


def test_validate_against_memory_below_floor_as_float_still_rejected(
    ehr_client: EHRClient,
) -> None:
    """F-002 regression: a float duration (30.0) must coerce and be rejected."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.memory = SessionMemory(
        identified_patient={"id": "patient-A"},
        last_slots=[{"slot_id": "slot-1"}],
        minimum_safe_minutes=60,
    )
    err = d._validate_against_memory(
        "create_appointment",
        {"slot_id": "slot-1", "patient_id": "patient-A", "duration_minutes": 30.0},
    )
    assert err is not None
    assert err.code == "below_minimum_safe_duration"


# ---------------------------------------------------------------------------
# _record_tool_result — Err path
# ---------------------------------------------------------------------------


def test_record_tool_result_err_appends_transcript_and_history(ehr_client: EHRClient) -> None:
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    err = Err(code="ehr_error", message="boom", retryable=True)
    d._record_tool_result("find_patient_by_phone", err)
    assert d.transcript[-1] == {
        "kind": "tool_err",
        "name": "find_patient_by_phone",
        "code": "ehr_error",
        "message": "boom",
        "retryable": True,
    }
    assert d.history[-1]["role"] == "tool"
    assert "ERROR code=ehr_error" in d.history[-1]["content"]


def test_record_tool_result_ok_create_patient_updates_memory(ehr_client: EHRClient) -> None:
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    ok = Ok(
        value={
            "patient_id": "pid-7",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "phone": "+12025550100",
            "dob": "1815-12-10",
        }
    )
    d._record_tool_result("create_patient", ok)
    # `dob` propagates so the operator-console `patient_identified` event
    # reports the real birth year — without it the year defaults to 0.
    assert d.memory.identified_patient == {
        "id": "pid-7",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "phone": "+12025550100",
        "dob": "1815-12-10",
    }


def test_record_tool_result_ok_find_single_patient_sets_identified(
    ehr_client: EHRClient,
) -> None:
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    p = {"id": "pid-7", "first_name": "Ada", "last_name": "Lovelace", "dob": "1990-12-10"}
    d._record_tool_result("find_patient_by_phone", Ok(value={"patients": [p]}))
    assert d.memory.identified_patient == p


def test_record_tool_result_ok_find_multiple_does_not_identify(
    ehr_client: EHRClient,
) -> None:
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    patients = [
        {"id": "pid-1", "first_name": "Ada", "last_name": "L", "dob": "1990-12-10"},
        {"id": "pid-2", "first_name": "Ada", "last_name": "L", "dob": "1990-12-10"},
    ]
    d._record_tool_result("find_patient_by_phone", Ok(value={"patients": patients}))
    # Ambiguous match — bot must ask DOB rather than autopick.
    assert d.memory.identified_patient is None


def test_record_tool_result_ok_get_upcoming_stores_in_memory(ehr_client: EHRClient) -> None:
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    appts = [{"id": "appt-1", "start_at": "x", "end_at": "y", "provider_name": "Dr."}]
    d._record_tool_result("get_upcoming_appointments", Ok(value={"appointments": appts}))
    assert d.memory.last_upcoming_appointments == appts


# ---------------------------------------------------------------------------
# Transition coverage — each TRANSITIONS edge
# ---------------------------------------------------------------------------


async def test_transition_no_match_routes_to_register(ehr_client: EHRClient) -> None:
    """IDENTIFY_PATIENT --no_match--> REGISTER_PATIENT via empty find_by_name_dob."""
    canned = CannedLLM(
        [
            LLMReply(text="hi"),
            LLMReply(
                text="",
                tool_calls=[
                    ToolCall(
                        name="find_patient_by_name_dob",
                        arguments={"name": "Nobody Here", "dob": "1900-01-01"},
                    )
                ],
            ),
            LLMReply(text="let's register you"),
        ]
    )
    async with ehr_client:
        d = Dispatcher(llm=canned, ehr_client=ehr_client)
        await d.start()
        await d.handle_user_turn("hi")
        assert d.state is State.REGISTER_PATIENT


async def test_transition_wants_cancel_routes_to_cancel_flow(ehr_client: EHRClient) -> None:
    canned = CannedLLM([LLMReply(text="hi")])
    async with ehr_client:
        d = Dispatcher(llm=canned, ehr_client=ehr_client)
        d.state = State.CHOOSE_INTENT
        await d.handle_user_turn("I'd like to cancel my appointment")
    assert d.state is State.CANCEL_FLOW


async def test_transition_wants_book_routes_to_book_flow(ehr_client: EHRClient) -> None:
    canned = CannedLLM([LLMReply(text="hi")])
    async with ehr_client:
        d = Dispatcher(llm=canned, ehr_client=ehr_client)
        d.state = State.CHOOSE_INTENT
        await d.handle_user_turn("I want to book a visit please")
    assert d.state is State.BOOK_FLOW


async def test_transition_cancel_nothing_to_cancel_routes_to_end(ehr_client: EHRClient) -> None:
    """CANCEL_FLOW with empty upcoming appointments must drop to END."""
    canned = CannedLLM(
        [
            LLMReply(
                text="",
                tool_calls=[
                    ToolCall(
                        name="get_upcoming_appointments",
                        arguments={"patient_id": "pid-no-appts"},
                    )
                ],
            ),
            LLMReply(text="nothing to cancel"),
        ]
    )
    async with ehr_client:
        d = Dispatcher(llm=canned, ehr_client=ehr_client)
        d.state = State.CANCEL_FLOW
        # patient with no appointments → get_upcoming returns [] → END
        # The EHR call will 404 for an unknown patient; we want the empty branch,
        # so seed memory + bypass by mocking memory only — but actually the tool
        # returns Err for unknown patient. Easier: drive transition directly.
        d._transition("nothing_to_cancel")
    assert d.state is State.END


def test_transition_abort_rolls_back_from_confirm_book(ehr_client: EHRClient) -> None:
    """CONFIRM_BOOK --abort--> BOOK_FLOW (user said no to confirmation)."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.CONFIRM_BOOK
    d._transition("abort")
    assert d.state is State.BOOK_FLOW


def test_transition_abort_rolls_back_from_confirm_cancel(ehr_client: EHRClient) -> None:
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.CONFIRM_CANCEL
    d._transition("abort")
    assert d.state is State.CANCEL_FLOW


def test_transition_unknown_label_is_noop(ehr_client: EHRClient) -> None:
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.GREETING
    d._transition("not_a_real_label")
    assert d.state is State.GREETING  # unchanged, no exception


def test_reschedule_intent_from_choose_intent_routes_to_reschedule_flow(
    ehr_client: EHRClient,
) -> None:
    """Caller saying 'reschedule' at CHOOSE_INTENT routes to the atomic
    RESCHEDULE_FLOW path, not the older cancel-then-rebook chain."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.CHOOSE_INTENT
    d._maybe_transition_from_user_text("I want to reschedule my appointment")
    assert d.state is State.RESCHEDULE_FLOW
    # Flag is cleared because the atomic path doesn't need the legacy
    # cancel-then-rebook auto-routing — the swap happens in one tool call.
    assert d.memory.wants_reschedule is False


def test_reschedule_intent_mid_call_still_sets_flag(ehr_client: EHRClient) -> None:
    """Mid-call 'reschedule' (after the caller is already in CANCEL_FLOW)
    keeps the legacy chain alive — the flag triggers BOOK_FLOW after the
    cancel completes."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.CANCEL_FLOW
    d._maybe_transition_from_user_text("actually I want to move my appointment instead")
    assert d.memory.wants_reschedule is True
    # State stays in CANCEL_FLOW — the flag is what changes the post-cancel
    # transition, not the current state.
    assert d.state is State.CANCEL_FLOW


def test_cancel_then_rebook_routes_to_book_flow(ehr_client: EHRClient) -> None:
    """CONFIRM_CANCEL + wants_reschedule + Ok → BOOK_FLOW (not END)."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.CONFIRM_CANCEL
    d.memory.wants_reschedule = True
    d._maybe_transition_from_tool(
        "cancel_appointment",
        Ok(value={"appointment_id": "abc"}),
    )
    assert d.state is State.BOOK_FLOW
    # Flag is consumed so a subsequent unrelated cancel still ends in END.
    assert d.memory.wants_reschedule is False


def test_plain_cancel_routes_to_end_not_book_flow(ehr_client: EHRClient) -> None:
    """Without wants_reschedule flag, cancel Ok still ends the call."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.CONFIRM_CANCEL
    assert d.memory.wants_reschedule is False
    d._maybe_transition_from_tool(
        "cancel_appointment",
        Ok(value={"appointment_id": "abc"}),
    )
    assert d.state is State.END


def test_hard_goodbye_fires_mid_utterance(ehr_client: EHRClient) -> None:
    """An explicit goodbye ('goodbye'/'hang up'/'end the call') routes to END
    from anywhere in the utterance (hard goodbye tier). Note: bare 'bye' is
    NOT hard-anywhere — mid-utterance it's the STT homophone of 'by the way'
    and must not hang up (audit F-012); it only ends at the utterance tail."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.IDENTIFY_PATIENT
    d._maybe_transition_from_user_text("ok goodbye let's stop here")
    assert d.state is State.END


def test_phone_words_to_digits_via_handler() -> None:
    """find_patient_by_phone normalises spoken digits before EHR lookup."""
    from prosper.tools import _phone_words_to_digits

    assert _phone_words_to_digits("two oh two five five five oh one zero zero") == ("2025550100")
    # Already-digit strings are passed through untouched.
    assert _phone_words_to_digits("2025550100") == "2025550100"
    # Too few digits → return original so EHR can reject with min_length error.
    assert _phone_words_to_digits("hello") == "hello"


def test_transition_book_flow_nothing_to_book_routes_to_end(ehr_client: EHRClient) -> None:
    """Exercises the BOOK_FLOW --nothing_to_book--> END edge in TRANSITIONS."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    d.state = State.BOOK_FLOW
    d._transition("nothing_to_book")
    assert d.state is State.END


# ---------------------------------------------------------------------------
# LLMUsage accumulation + sliding-window history cap
# ---------------------------------------------------------------------------


async def test_dispatcher_accumulates_cached_and_prompt_tokens(ehr_client: EHRClient) -> None:
    canned = CannedLLM(
        [
            LLMReply(text="hi", usage=LLMUsage(prompt_tokens=100, cached_prompt_tokens=80)),
            LLMReply(text="ok", usage=LLMUsage(prompt_tokens=120, cached_prompt_tokens=100)),
        ]
    )
    async with ehr_client:
        d = Dispatcher(llm=canned, ehr_client=ehr_client)
        await d.start()
        await d.handle_user_turn("hi")
    assert d.cached_prompt_tokens_total == 180
    assert d.prompt_tokens_total == 220


# ---------------------------------------------------------------------------
# _maybe_transition_from_tool — every state/tool/result combo
# ---------------------------------------------------------------------------


def _make_dispatcher(ehr_client: EHRClient, state: State) -> Dispatcher:
    d = Dispatcher(llm=CannedLLM([]), ehr_client=ehr_client)
    d.state = state
    return d


def test_transition_from_tool_identify_single_match(ehr_client: EHRClient) -> None:
    d = _make_dispatcher(ehr_client, State.IDENTIFY_PATIENT)
    d._maybe_transition_from_tool(
        "find_patient_by_phone",
        Ok(value={"patients": [{"id": "pid-1"}]}),
    )
    assert d.state is State.CHOOSE_INTENT


def test_transition_from_tool_identify_name_dob_no_match(ehr_client: EHRClient) -> None:
    d = _make_dispatcher(ehr_client, State.IDENTIFY_PATIENT)
    d._maybe_transition_from_tool("find_patient_by_name_dob", Ok(value={"patients": []}))
    assert d.state is State.REGISTER_PATIENT


def test_transition_from_tool_identify_phone_no_match_stays_put(ehr_client: EHRClient) -> None:
    """Phone miss doesn't auto-route; only name+dob miss does (so bot can ask DOB)."""
    d = _make_dispatcher(ehr_client, State.IDENTIFY_PATIENT)
    d._maybe_transition_from_tool("find_patient_by_phone", Ok(value={"patients": []}))
    assert d.state is State.IDENTIFY_PATIENT


def test_transition_from_tool_register_patient_routes_to_choose(ehr_client: EHRClient) -> None:
    d = _make_dispatcher(ehr_client, State.REGISTER_PATIENT)
    d._maybe_transition_from_tool("create_patient", Ok(value={"patient_id": "pid-1"}))
    assert d.state is State.CHOOSE_INTENT


def test_transition_from_tool_book_flow_routes_to_confirm(ehr_client: EHRClient) -> None:
    d = _make_dispatcher(ehr_client, State.BOOK_FLOW)
    d._maybe_transition_from_tool(
        "list_availability_slots", Ok(value={"slots": [{"slot_id": "s-1"}]})
    )
    assert d.state is State.CONFIRM_BOOK


def test_transition_from_tool_cancel_with_appts_routes_to_confirm(
    ehr_client: EHRClient,
) -> None:
    d = _make_dispatcher(ehr_client, State.CANCEL_FLOW)
    d._maybe_transition_from_tool(
        "get_upcoming_appointments", Ok(value={"appointments": [{"id": "a-1"}]})
    )
    assert d.state is State.CONFIRM_CANCEL


def test_transition_from_tool_cancel_with_no_appts_routes_to_end(
    ehr_client: EHRClient,
) -> None:
    d = _make_dispatcher(ehr_client, State.CANCEL_FLOW)
    d._maybe_transition_from_tool("get_upcoming_appointments", Ok(value={"appointments": []}))
    assert d.state is State.END


def test_transition_from_tool_confirm_book_routes_to_end(ehr_client: EHRClient) -> None:
    d = _make_dispatcher(ehr_client, State.CONFIRM_BOOK)
    d._maybe_transition_from_tool("create_appointment", Ok(value={"appointment_id": "a"}))
    assert d.state is State.END


def test_transition_from_tool_confirm_cancel_routes_to_end(ehr_client: EHRClient) -> None:
    d = _make_dispatcher(ehr_client, State.CONFIRM_CANCEL)
    d._maybe_transition_from_tool("cancel_appointment", Ok(value={"ok": True}))
    assert d.state is State.END


def test_transition_from_tool_err_does_not_advance(ehr_client: EHRClient) -> None:
    """An Err result must NOT trigger a state transition — bot must retry/recover."""
    d = _make_dispatcher(ehr_client, State.CONFIRM_BOOK)
    d._maybe_transition_from_tool(
        "create_appointment",
        Err(code="slot_taken_other_patient", message="x", retryable=True),
    )
    assert d.state is State.CONFIRM_BOOK


def test_messages_for_llm_caps_history_window(ehr_client: EHRClient) -> None:
    """The dispatcher must not grow the LLM prompt linearly on long calls."""
    canned = CannedLLM([])
    d = Dispatcher(llm=canned, ehr_client=ehr_client)
    # Push 100 history turns past the 40-message cap.
    for i in range(100):
        d.history.append({"role": "user", "content": f"turn {i}"})
    msgs = d._messages_for_llm()
    # 2 system messages (persona + task) + 40 recent history entries
    assert len(msgs) == 42
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "system"
    # The first kept history entry should be turn 60 (the last 40 of 0..99)
    assert msgs[2]["content"] == "turn 60"
    assert msgs[-1]["content"] == "turn 99"


# ---------------------------------------------------------------------------
# Bug-fuzz regression: dispatcher must NOT crash if the LLM emits an extra
# unknown kwarg on a tool call (e.g. ``mystery_field=42``). Previously this
# raised ``TypeError: ... got an unexpected keyword argument`` from the
# handler invocation, killing the whole turn.
# ---------------------------------------------------------------------------


async def test_execute_tool_strips_unknown_kwargs(ehr_client: EHRClient) -> None:
    canned = CannedLLM([])
    async with ehr_client:
        d = Dispatcher(llm=canned, ehr_client=ehr_client)
        d.state = State.CONFIRM_BOOK
        # Seed memory so the hallucinated-id guard passes.
        d.memory.last_slots = [{"slot_id": "abc"}]
        d.memory.identified_patient = {"id": "pid-1"}
        call = ToolCall(
            name="create_appointment",
            arguments={
                "slot_id": "abc",
                "patient_id": "pid-1",
                "mystery_field": 42,
                "another_bad_one": "x",
            },
        )
        # Must not raise; returns an Err (patient/slot not found because the
        # ids are fake) — the point is the handler invocation itself survives.
        result = await d._execute_tool(call)
    assert result.kind == "err"
    # The dispatcher's hallucination guard fires first ("abc" is in known_slots
    # but the fake ids never reach the EHR cleanly); accept any Err code so the
    # test stays focused on the kwarg-filter behaviour.


async def test_end_state_blocks_user_text_transitions(ehr_client: EHRClient) -> None:
    """Once the FSM reaches END, no user_text should escape it (no transitions)."""
    canned = CannedLLM([])
    async with ehr_client:
        d = Dispatcher(llm=canned, ehr_client=ehr_client)
        d.state = State.END
        d._maybe_transition_from_user_text("yes I want to book another")
        d._maybe_transition_from_user_text("cancel that")
        d._maybe_transition_from_user_text("hello?")
    assert d.state is State.END


def test_transition_from_tool_book_flow_empty_slots_stays_put(
    ehr_client: EHRClient,
) -> None:
    """Bug 1 regression: an empty ``slots: []`` result must NOT advance to
    CONFIRM_BOOK. Otherwise the FSM gets stranded with empty ``last_slots``
    and every subsequent create_appointment fails ``hallucinated_slot_id``.
    """
    d = _make_dispatcher(ehr_client, State.BOOK_FLOW)
    d._maybe_transition_from_tool("list_availability_slots", Ok(value={"slots": []}))
    assert d.state is State.BOOK_FLOW
    # An observability breadcrumb is recorded so reviewers can spot the case.
    assert any(e.get("kind") == "empty_slot_result" for e in d.transcript)


async def test_never_mind_mid_sentence_does_not_trigger_goodbye(
    ehr_client: EHRClient,
) -> None:
    """Bug 2 regression: ``never mind`` mid-utterance must not route to END.

    Previously the goodbye regex matched ``never mind`` anywhere, so a caller
    saying "never mind the insurance, I want to book" during CHOOSE_INTENT was
    silently dropped to END mid-flow. The trailing-anchor split fixes this.
    """
    canned = CannedLLM([LLMReply(text="ok, what day works for you?")])
    async with ehr_client:
        d = Dispatcher(llm=canned, ehr_client=ehr_client)
        d.state = State.CHOOSE_INTENT
        await d.handle_user_turn("never mind the insurance, I want to book")
    # Booking intent in the same utterance wins; END is not reached.
    assert d.state is State.BOOK_FLOW
    # And a trailing "never mind" still routes to END from CHOOSE_INTENT.
    async with ehr_client:
        d2 = Dispatcher(llm=CannedLLM([LLMReply(text="okay, bye")]), ehr_client=ehr_client)
        d2.state = State.CHOOSE_INTENT
        await d2.handle_user_turn("actually, never mind.")
    assert d2.state is State.END


def test_mark_last_assistant_interrupted_truncates_and_marks(
    ehr_client: EHRClient,
) -> None:
    """Subproblem A (2026-05-23): the dispatcher must rewrite the last
    assistant turn to reflect only the audible portion plus the
    [INTERRUPTED by user] marker so the next LLM call sees an honest
    timeline. See docs/research/interruption_design.md."""
    d = _make_dispatcher(ehr_client, State.CONFIRM_BOOK)
    d.history.append({"role": "user", "content": "ten o'clock works"})
    d.history.append(
        {
            "role": "assistant",
            "content": "Booking 2 PM with Dr. Smith on Tuesday — confirming now.",
        }
    )
    d.mark_last_assistant_interrupted("Booking 2 PM with Dr. Smi")
    last = d.history[-1]
    assert "[INTERRUPTED by user]" in last["content"]
    assert last["content"].startswith("Booking 2 PM with Dr. Smi")
    # Previous user turn is untouched.
    assert d.history[-2]["content"] == "ten o'clock works"


def test_mark_last_assistant_interrupted_handles_zero_spoken_text(
    ehr_client: EHRClient,
) -> None:
    """Interrupt that fires before any TTS text propagated → mark the turn
    as [NOT HEARD] so the LLM knows the caller heard nothing."""
    d = _make_dispatcher(ehr_client, State.IDENTIFY_PATIENT)
    d.history.append({"role": "assistant", "content": "What's the best phone number..."})
    d.mark_last_assistant_interrupted("")
    last = d.history[-1]
    assert "[NOT HEARD]" in last["content"]
    assert "[INTERRUPTED by user]" in last["content"]


def test_mark_last_assistant_interrupted_noop_when_last_role_not_assistant(
    ehr_client: EHRClient,
) -> None:
    """Race: if the user turn already landed in history before the interrupt
    flushes (or there's no history yet), the marker must not corrupt an
    unrelated entry."""
    d = _make_dispatcher(ehr_client, State.GREETING)
    # Empty history — must be a no-op, not an IndexError.
    d.mark_last_assistant_interrupted("anything")
    assert d.history == []
    # User as last role — must remain unchanged.
    d.history.append({"role": "user", "content": "hello there"})
    d.mark_last_assistant_interrupted("anything")
    assert d.history[-1] == {"role": "user", "content": "hello there"}


def test_mark_last_assistant_interrupted_is_idempotent(ehr_client: EHRClient) -> None:
    """Two flushes for the same turn (paranoid frame doubling) must not
    cascade the marker into a giant string."""
    d = _make_dispatcher(ehr_client, State.BOOK_FLOW)
    d.history.append({"role": "assistant", "content": "Let me check what's available"})
    d.mark_last_assistant_interrupted("Let me check")
    after_first = d.history[-1]["content"]
    d.mark_last_assistant_interrupted("Let me check again")
    assert d.history[-1]["content"] == after_first


async def test_interrupted_timeline_reaches_next_llm_call(ehr_client: EHRClient) -> None:
    """End-to-end timeline contract: an interrupt between two user turns must
    surface to the LLM as a truncated+marked assistant entry followed by the
    barge-in utterance, in order. This is the honest-timeline the LLM needs to
    decide whether the caller is reacting to the cut line or an earlier turn."""

    class HistoryCapturingLLM(LLMClientProtocol):
        def __init__(self, replies: list[LLMReply]) -> None:
            self._iter: Iterator[LLMReply] = iter(replies)
            self.last_history: list[dict[str, Any]] = []

        async def generate(
            self, *, state: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]
        ) -> LLMReply:
            self.last_history = list(history)
            try:
                return next(self._iter)
            except StopIteration:
                return LLMReply(text="", tool_calls=[])

    canned = HistoryCapturingLLM(
        [
            LLMReply(text="Booking 2 PM with Dr. Smith on Tuesday — confirming now."),
            LLMReply(text="No problem — what day would you prefer instead?"),
        ]
    )
    async with ehr_client:
        d = Dispatcher(llm=canned, ehr_client=ehr_client)
        d.state = State.CONFIRM_BOOK
        # Turn 1: caller agrees, bot starts reading back the booking.
        await d.handle_user_turn("yes that works")
        # VAD interrupts mid-readback — observer flushes the audible prefix.
        d.mark_last_assistant_interrupted("Booking 2 PM with Dr. Smi")
        # Turn 2: caller cuts in to change their mind.
        await d.handle_user_turn("wait no, not Tuesday")

    # The LLM's view on turn 2 must contain the marked assistant turn BEFORE
    # the barge-in user utterance, in timeline order.
    contents = [m.get("content", "") for m in canned.last_history]
    marked = next((c for c in contents if "[INTERRUPTED by user]" in c), None)
    assert marked is not None, "interrupted assistant turn missing from LLM history"
    assert "wait no, not Tuesday" in contents
    assert contents.index(marked) < contents.index("wait no, not Tuesday")


# --- Subproblem C: fuzzy multi-candidate identity disambiguation -------------

_TWO_SMITHS = [
    {
        "id": "p1",
        "first_name": "John",
        "last_name": "Smith",
        "dob": "1985-03-04",
        "similarity": 0.9,
    },
    {
        "id": "p2",
        "first_name": "Jon",
        "last_name": "Smith",
        "dob": "1985-03-04",
        "similarity": 0.88,
    },
]


def test_redact_find_multiple_candidates_numbered() -> None:
    """A multi-candidate find result is rendered as a numbered, UUID-free
    list so the caller can pick by number."""
    out = _redact_for_llm("find_patient_by_name_dob", {"patients": _TWO_SMITHS})
    assert "[1] John Smith (DOB 1985-03-04)" in out
    assert "[2] Jon Smith (DOB 1985-03-04)" in out
    assert "p1" not in out and "p2" not in out  # no ids leak


def test_identify_multiple_candidates_holds_disambiguation(ehr_client: EHRClient) -> None:
    """Two same-DOB / similar-name matches must NOT auto-pick an identity.
    The dispatcher stays in IDENTIFY_PATIENT with candidates pending."""
    d = _make_dispatcher(ehr_client, State.IDENTIFY_PATIENT)
    result = Ok(value={"patients": _TWO_SMITHS})
    d._record_tool_result("find_patient_by_name_dob", result)
    d._maybe_transition_from_tool("find_patient_by_name_dob", result)
    assert d.state is State.IDENTIFY_PATIENT
    assert d.memory.identified_patient is None
    assert len(d.memory.pending_identity_candidates) == 2


async def test_identify_pick_by_ordinal_resolves(ehr_client: EHRClient) -> None:
    """'the first one' resolves to candidate[0] and advances to CHOOSE_INTENT."""
    async with ehr_client:
        d = Dispatcher(
            llm=CannedLLM([LLMReply(text="great, book or cancel?")]), ehr_client=ehr_client
        )
        d.state = State.IDENTIFY_PATIENT
        d.memory.pending_identity_candidates = list(_TWO_SMITHS)
        await d.handle_user_turn("the first one")
    assert d.memory.identified_patient is not None
    assert d.memory.identified_patient["id"] == "p1"
    assert d.memory.pending_identity_candidates == []
    assert d.state is State.CHOOSE_INTENT


async def test_identify_pick_by_number_resolves_second(ehr_client: EHRClient) -> None:
    """'number two' resolves to candidate[1]."""
    async with ehr_client:
        d = Dispatcher(llm=CannedLLM([LLMReply(text="ok!")]), ehr_client=ehr_client)
        d.state = State.IDENTIFY_PATIENT
        d.memory.pending_identity_candidates = list(_TWO_SMITHS)
        await d.handle_user_turn("number two please")
    assert d.memory.identified_patient["id"] == "p2"
    assert d.state is State.CHOOSE_INTENT


async def test_identify_unparseable_pick_stays_pending(ehr_client: EHRClient) -> None:
    """If the caller's reply doesn't select a candidate, stay in IDENTIFY so
    the LLM asks again — never guess an identity."""
    async with ehr_client:
        d = Dispatcher(llm=CannedLLM([LLMReply(text="sorry, which one?")]), ehr_client=ehr_client)
        d.state = State.IDENTIFY_PATIENT
        d.memory.pending_identity_candidates = list(_TWO_SMITHS)
        await d.handle_user_turn("um, I'm not totally sure")
    assert d.memory.identified_patient is None
    assert len(d.memory.pending_identity_candidates) == 2
    assert d.state is State.IDENTIFY_PATIENT


def test_identify_single_fuzzy_match_still_auto_advances(ehr_client: EHRClient) -> None:
    """Regression guard: a single match (even fuzzy) keeps the existing
    auto-advance behavior — only the >1 case is held for disambiguation."""
    d = _make_dispatcher(ehr_client, State.IDENTIFY_PATIENT)
    one = [
        {
            "id": "p9",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "dob": "1990-12-10",
            "similarity": 0.9,
        }
    ]
    result = Ok(value={"patients": one})
    d._record_tool_result("find_patient_by_phone", result)
    d._maybe_transition_from_tool("find_patient_by_phone", result)
    assert d.memory.identified_patient is not None
    assert d.state is State.CHOOSE_INTENT
