"""The state graph is data, not behaviour — these tests are about shape only."""

from prosper.flows import ALLOWED_TOOLS, STATES, TRANSITIONS, State


def test_all_states_present() -> None:
    assert set(STATES) == {
        State.GREETING,
        State.IDENTIFY_PATIENT,
        State.REGISTER_PATIENT,
        State.CHOOSE_INTENT,
        State.BOOK_FLOW,
        State.CANCEL_FLOW,
        State.RESCHEDULE_FLOW,
        State.CONFIRM_BOOK,
        State.CONFIRM_CANCEL,
        State.CONFIRM_RESCHEDULE,
        State.END,
    }


def test_tool_whitelist_per_state() -> None:
    assert ALLOWED_TOOLS[State.GREETING] == set()
    assert ALLOWED_TOOLS[State.IDENTIFY_PATIENT] == {
        "find_patient_by_phone",
        "find_patient_by_name_dob",
    }
    assert ALLOWED_TOOLS[State.REGISTER_PATIENT] == {"create_patient"}
    assert ALLOWED_TOOLS[State.CHOOSE_INTENT] == {"route_intent"}
    assert ALLOWED_TOOLS[State.BOOK_FLOW] == {"list_availability_slots", "suggest_specialty"}
    assert ALLOWED_TOOLS[State.CANCEL_FLOW] == {"get_upcoming_appointments"}
    assert ALLOWED_TOOLS[State.RESCHEDULE_FLOW] == {
        "get_upcoming_appointments",
        "list_availability_slots",
    }
    assert ALLOWED_TOOLS[State.CONFIRM_BOOK] == {"create_appointment"}
    assert ALLOWED_TOOLS[State.CONFIRM_CANCEL] == {"cancel_appointment"}
    assert ALLOWED_TOOLS[State.CONFIRM_RESCHEDULE] == {"reschedule_appointment"}
    assert ALLOWED_TOOLS[State.END] == set()


def test_transitions_form_valid_graph() -> None:
    for src, targets in TRANSITIONS.items():
        for label, dst in targets.items():
            assert dst in STATES, f"{src} --{label}--> {dst} is unknown"


def test_no_state_can_reach_a_write_tool_directly() -> None:
    writes = {
        "create_patient",
        "create_appointment",
        "cancel_appointment",
        "reschedule_appointment",
    }
    for state, tools in ALLOWED_TOOLS.items():
        if state in (
            State.CONFIRM_BOOK,
            State.CONFIRM_CANCEL,
            State.CONFIRM_RESCHEDULE,
            State.REGISTER_PATIENT,
        ):
            continue
        assert tools.isdisjoint(writes), f"{state} can write directly"
