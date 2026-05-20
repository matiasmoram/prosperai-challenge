"""Six base scenarios that exercise the spec's mandatory flows."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from evals.types import Scenario, StateExpectation
from prosper.ehr import repository as repo
from prosper.ehr.models import Patient, Provider, Slot


def _seed_provider_and_slots(session: Session, *, count: int = 4) -> tuple[Provider, list[Slot]]:
    prov = Provider(name="Dr. Patel", timezone="UTC")
    session.add(prov)
    session.commit()
    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    slots: list[Slot] = []
    for i in range(count):
        s = Slot(
            provider_id=prov.id,
            start_at=start + timedelta(minutes=30 * i),
            end_at=start + timedelta(minutes=30 * (i + 1)),
        )
        session.add(s)
        slots.append(s)
    session.commit()
    return prov, slots


def _seed_existing_patient(session: Session, *, phone: str = "+12025550100") -> Patient:
    return repo.create_patient(
        session,
        first_name="Ada",
        last_name="Lovelace",
        dob=date(1990, 12, 10),
        phone=phone,
    )


def _setup_new_patient_books(session: Session) -> None:
    _seed_provider_and_slots(session)


def _setup_existing_one_appt(session: Session) -> None:
    _, slots = _seed_provider_and_slots(session, count=4)
    patient = _seed_existing_patient(session)
    repo.create_appointment(session, patient_id=patient.id, slot_id=slots[0].id)


def _setup_existing_three_appts(session: Session) -> None:
    _, slots = _seed_provider_and_slots(session, count=6)
    patient = _seed_existing_patient(session)
    for s in slots[:3]:
        repo.create_appointment(session, patient_id=patient.id, slot_id=s.id)


def _setup_existing_no_appts(session: Session) -> None:
    _seed_provider_and_slots(session)
    _seed_existing_patient(session)


def _setup_slot_taken_by_other(session: Session) -> None:
    _, slots = _seed_provider_and_slots(session, count=4)
    holder = repo.create_patient(
        session,
        first_name="Other",
        last_name="Holder",
        dob=date(1980, 1, 1),
        phone="+15551110000",
    )
    repo.create_appointment(session, patient_id=holder.id, slot_id=slots[0].id)


SCENARIOS: list[Scenario] = [
    Scenario(
        name="new_patient_books",
        tags=frozenset({"happy"}),
        persona=(
            "You are a NEW caller named Test User, DOB 1 January 1990, phone "
            "555-999-9999. You want to book any morning slot tomorrow. Provide "
            "the phone first if asked, then full name and DOB. Confirm clearly "
            "when the bot reads things back."
        ),
        setup=_setup_new_patient_books,
        expected_state=StateExpectation(
            patient_count_delta=1,
            active_appointment_count_delta=1,
            expected_terminal_state="END",
            expected_tool_call_codes=[
                "find_patient_by_phone",
                "create_patient",
                "list_availability_slots",
                "create_appointment",
            ],
        ),
        judge_criteria=[
            "the bot asked for the caller's phone number before any other identifier",
            "the bot confirmed the chosen time and provider aloud before booking",
            "the bot ended the call after a successful booking",
        ],
        max_turns=16,
    ),
    Scenario(
        name="existing_patient_cancels",
        tags=frozenset({"happy"}),
        persona=(
            "You are Ada Lovelace, DOB December 10 1990, phone 202-555-0100. "
            "You have a single upcoming appointment. Politely cancel it. "
            "Confirm yes when the bot reads the details back."
        ),
        setup=_setup_existing_one_appt,
        expected_state=StateExpectation(
            patient_count_delta=0,
            active_appointment_count_delta=-1,
            cancelled_appointment_count_delta=1,
            expected_terminal_state="END",
            expected_tool_call_codes=[
                "find_patient_by_phone",
                "get_upcoming_appointments",
                "cancel_appointment",
            ],
            forbidden_tool_calls=["create_patient", "create_appointment"],
        ),
        judge_criteria=[
            "the bot identified the caller via phone before discussing appointments",
            "the bot read back the specific appointment before cancelling",
            "no new appointment was booked during this call",
        ],
        max_turns=12,
    ),
    Scenario(
        name="cancel_picks_from_list",
        tags=frozenset({"happy"}),
        persona=(
            "You are Ada Lovelace, DOB December 10 1990, phone 202-555-0100. "
            "You have three upcoming appointments. Ask to cancel the SECOND "
            "one on the bot's numbered list. Confirm yes."
        ),
        setup=_setup_existing_three_appts,
        expected_state=StateExpectation(
            patient_count_delta=0,
            active_appointment_count_delta=-1,
            cancelled_appointment_count_delta=1,
            expected_terminal_state="END",
        ),
        judge_criteria=[
            "the bot read out a numbered list of upcoming appointments",
            "the bot cancelled the appointment matching the caller's choice (number two)",
        ],
        max_turns=12,
    ),
    Scenario(
        name="dob_misheard_then_corrected",
        tags=frozenset({"recovery"}),
        persona=(
            "You are a NEW caller named Sam Patel. Your real DOB is March 3 "
            "1985. On the first try, when asked for your DOB, SAY 'March third "
            "nineteen eighty-FIVE' but mis-pronounce the year as 'nineteen "
            "eighty-six'. When the bot reads it back, correct it to 1985. "
            "Phone is 555-111-2222. Book any tomorrow morning slot."
        ),
        setup=_setup_new_patient_books,
        expected_state=StateExpectation(
            patient_count_delta=1,
            active_appointment_count_delta=1,
            expected_terminal_state="END",
        ),
        judge_criteria=[
            "the bot read back the DOB for confirmation",
            "the bot accepted the caller's correction without confusion",
            "the patient record was created with the corrected DOB",
        ],
        max_turns=18,
    ),
    Scenario(
        name="slot_taken_by_other",
        tags=frozenset({"edge"}),
        persona=(
            "You are a NEW caller named Mia Wong, DOB June 5 1992, phone "
            "555-777-8888. You insist on booking the very first slot the bot "
            "lists. When the bot tries to book and reports a conflict, ask "
            "for an alternative time and accept the next slot offered."
        ),
        setup=_setup_slot_taken_by_other,
        expected_state=StateExpectation(
            patient_count_delta=1,
            active_appointment_count_delta=1,
            expected_terminal_state="END",
        ),
        judge_criteria=[
            "the bot acknowledged the slot conflict and offered alternatives",
            "an alternative slot was successfully booked for the new patient",
        ],
        max_turns=16,
    ),
    Scenario(
        name="cancel_when_nothing_to_cancel",
        tags=frozenset({"edge"}),
        persona=(
            "You are Ada Lovelace, DOB December 10 1990, phone 202-555-0100. "
            "You ask to cancel an appointment but actually have nothing on "
            "the calendar. When the bot says so, simply say goodbye."
        ),
        setup=_setup_existing_no_appts,
        expected_state=StateExpectation(
            patient_count_delta=0,
            active_appointment_count_delta=0,
            cancelled_appointment_count_delta=0,
            expected_terminal_state="END",
            expected_tool_call_codes=["get_upcoming_appointments"],
            forbidden_tool_calls=[
                "cancel_appointment",
                "create_appointment",
                "create_patient",
            ],
        ),
        judge_criteria=[
            "the bot stated clearly that there were no upcoming appointments",
            "the bot did not invent an appointment or attempt to cancel anything",
        ],
        max_turns=10,
    ),
]
