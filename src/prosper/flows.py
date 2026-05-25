"""FSM state graph as plain data — states, per-state tool whitelist, edges.

Transition decisions are made by the dispatcher (which inspects tool result
codes and identifies short-circuit keywords in the LLM reply). This module
only defines the topology.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping


class State(str, enum.Enum):
    GREETING = "GREETING"
    IDENTIFY_PATIENT = "IDENTIFY_PATIENT"
    REGISTER_PATIENT = "REGISTER_PATIENT"
    CHOOSE_INTENT = "CHOOSE_INTENT"
    BOOK_FLOW = "BOOK_FLOW"
    CANCEL_FLOW = "CANCEL_FLOW"
    RESCHEDULE_FLOW = "RESCHEDULE_FLOW"
    CONFIRM_BOOK = "CONFIRM_BOOK"
    CONFIRM_CANCEL = "CONFIRM_CANCEL"
    CONFIRM_RESCHEDULE = "CONFIRM_RESCHEDULE"
    # Terminal state: caller has been handed off to the human front desk.
    # Reached via the `needs_human` transition from any post-identity flow state.
    HANDOFF = "HANDOFF"
    END = "END"


STATES: tuple[State, ...] = tuple(State)


ALLOWED_TOOLS: dict[State, set[str]] = {
    State.GREETING: set(),
    # ``leave_message_for_front_desk`` is whitelisted here too so a caller
    # who insists they are already in the system (but DOB keeps failing) can
    # be handed off to the front desk during identification rather than being
    # forced into new-patient registration. The tool is dispatcher-intercepted
    # (no HANDLERS entry) — identity comes from whatever is already in
    # SessionMemory, falling back to the LLM-collected name/phone in the summary.
    State.IDENTIFY_PATIENT: {
        "find_patient_by_phone",
        "find_patient_by_name_dob",
        "leave_message_for_front_desk",
    },
    # ``leave_message_for_front_desk`` in REGISTER_PATIENT allows the bot to
    # hand off a caller who reached registration but is actually an existing
    # patient whose record cannot be found (e.g. registered under a different
    # phone). Same intercepted pattern — avoids creating a duplicate patient.
    State.REGISTER_PATIENT: {"create_patient", "leave_message_for_front_desk"},
    # ``route_intent`` is the HYBRID navigation tool: the LLM proposes the
    # caller's intent and the dispatcher validates the edge (see
    # ``Dispatcher._handle_route_intent``). It is whitelisted here but handled
    # internally — it has no EHR handler in ``HANDLERS`` (see ``INTERNAL_TOOLS``).
    # ``leave_message_for_front_desk`` is whitelisted in the four post-identity
    # flow states but handled internally (no EHR call, needs SessionMemory +
    # MailStore). See ``INTERNAL_TOOLS`` and ``Dispatcher._handle_leave_message``.
    State.CHOOSE_INTENT: {"route_intent", "leave_message_for_front_desk"},
    State.BOOK_FLOW: {
        "list_availability_slots",
        "suggest_specialty",
        "leave_message_for_front_desk",
    },
    State.CANCEL_FLOW: {"get_upcoming_appointments", "leave_message_for_front_desk"},
    # Reschedule needs BOTH lookups in one state so the bot can pick the
    # old appointment AND the new slot before committing. The atomic
    # ``reschedule_appointment`` tool then fires in CONFIRM_RESCHEDULE.
    State.RESCHEDULE_FLOW: {
        "get_upcoming_appointments",
        "list_availability_slots",
        "leave_message_for_front_desk",
    },
    State.CONFIRM_BOOK: {"create_appointment"},
    State.CONFIRM_CANCEL: {"cancel_appointment"},
    State.CONFIRM_RESCHEDULE: {"reschedule_appointment"},
    # HANDOFF is terminal: the caller has been handed off to the front desk.
    # No tools are available — the bot speaks the HANDOFF task message then
    # the FSM advances to END on the next goodbye.
    State.HANDOFF: set(),
    State.END: set(),
}


# Tools the dispatcher handles internally (no EHR call, no latency, no entry in
# ``tools.HANDLERS``). They are whitelisted in ``ALLOWED_TOOLS`` so the LLM can
# call them, but ``Dispatcher._llm_turn`` intercepts them before ``_execute_tool``.
# Kept here so both the dispatcher (interception) and ``bot._should_emit_filler``
# (which must not predict latency for a tool that fires none) share one source.
# ``leave_message_for_front_desk`` is intercepted because it needs SessionMemory +
# the injected MailStore rather than the EHR client.
INTERNAL_TOOLS: frozenset[str] = frozenset({"route_intent", "leave_message_for_front_desk"})


TRANSITIONS: Mapping[State, Mapping[str, State]] = {
    State.GREETING: {"go_identify": State.IDENTIFY_PATIENT, "goodbye": State.END},
    State.IDENTIFY_PATIENT: {
        "patient_found": State.CHOOSE_INTENT,
        "no_match": State.REGISTER_PATIENT,
        # Caller insists they are already in the system but name+DOB never matches
        # → bot hands off to the front desk rather than forcing registration.
        "needs_human": State.HANDOFF,
        "goodbye": State.END,
    },
    State.REGISTER_PATIENT: {
        "registered": State.CHOOSE_INTENT,
        # Caller reached REGISTER_PATIENT but turns out to be an existing patient
        # whose record cannot be found → hand off rather than create a duplicate.
        "needs_human": State.HANDOFF,
        "goodbye": State.END,
    },
    State.CHOOSE_INTENT: {
        "wants_book": State.BOOK_FLOW,
        "wants_cancel": State.CANCEL_FLOW,
        "wants_reschedule": State.RESCHEDULE_FLOW,
        "needs_human": State.HANDOFF,
        "goodbye": State.END,
    },
    State.BOOK_FLOW: {
        "slot_chosen": State.CONFIRM_BOOK,
        "nothing_to_book": State.END,
        # Triage red flag (suggest_specialty → medical_emergency): unmount the
        # booking tools immediately so the agent cannot book a routine visit
        # for a caller in a medical emergency — the 911 redirect is then a
        # hard FSM guarantee, not just prompt guidance (audit F-011).
        "medical_emergency": State.END,
        "needs_human": State.HANDOFF,
        "goodbye": State.END,
    },
    State.CANCEL_FLOW: {
        "appointment_chosen": State.CONFIRM_CANCEL,
        "nothing_to_cancel": State.END,
        "needs_human": State.HANDOFF,
        "goodbye": State.END,
    },
    State.RESCHEDULE_FLOW: {
        # Both lookups satisfied → confirm the atomic swap.
        "slot_chosen": State.CONFIRM_RESCHEDULE,
        # No upcoming appointments to move — wrap up gracefully.
        "nothing_to_reschedule": State.END,
        "needs_human": State.HANDOFF,
        "goodbye": State.END,
    },
    State.CONFIRM_BOOK: {
        "booked": State.END,
        "abort": State.BOOK_FLOW,
        "goodbye": State.END,
    },
    State.CONFIRM_CANCEL: {
        "cancelled": State.END,
        "cancelled_then_rebook": State.BOOK_FLOW,
        "abort": State.CANCEL_FLOW,
        "goodbye": State.END,
    },
    State.CONFIRM_RESCHEDULE: {
        "rescheduled": State.END,
        # Backed out of confirmation — let the caller pick a different
        # appointment or slot inside RESCHEDULE_FLOW.
        "abort": State.RESCHEDULE_FLOW,
        "goodbye": State.END,
    },
    # HANDOFF is terminal but has one more hop — the bot speaks its callback
    # confirmation line then the call ends when the caller says goodbye.
    State.HANDOFF: {
        "handed_off": State.END,
        "goodbye": State.END,
    },
    State.END: {},
}
