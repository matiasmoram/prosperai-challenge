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
    CONFIRM_BOOK = "CONFIRM_BOOK"
    CONFIRM_CANCEL = "CONFIRM_CANCEL"
    END = "END"


STATES: tuple[State, ...] = tuple(State)


ALLOWED_TOOLS: dict[State, set[str]] = {
    State.GREETING: set(),
    State.IDENTIFY_PATIENT: {"find_patient_by_phone", "find_patient_by_name_dob"},
    State.REGISTER_PATIENT: {"create_patient"},
    State.CHOOSE_INTENT: set(),
    State.BOOK_FLOW: {"list_availability_slots"},
    State.CANCEL_FLOW: {"get_upcoming_appointments"},
    State.CONFIRM_BOOK: {"create_appointment"},
    State.CONFIRM_CANCEL: {"cancel_appointment"},
    State.END: set(),
}


TRANSITIONS: Mapping[State, Mapping[str, State]] = {
    State.GREETING: {"go_identify": State.IDENTIFY_PATIENT},
    State.IDENTIFY_PATIENT: {
        "patient_found": State.CHOOSE_INTENT,
        "no_match": State.REGISTER_PATIENT,
    },
    State.REGISTER_PATIENT: {"registered": State.CHOOSE_INTENT},
    State.CHOOSE_INTENT: {
        "wants_book": State.BOOK_FLOW,
        "wants_cancel": State.CANCEL_FLOW,
    },
    State.BOOK_FLOW: {
        "slot_chosen": State.CONFIRM_BOOK,
        "nothing_to_book": State.END,
    },
    State.CANCEL_FLOW: {
        "appointment_chosen": State.CONFIRM_CANCEL,
        "nothing_to_cancel": State.END,
    },
    State.CONFIRM_BOOK: {"booked": State.END, "abort": State.BOOK_FLOW},
    State.CONFIRM_CANCEL: {"cancelled": State.END, "abort": State.CANCEL_FLOW},
    State.END: {},
}
