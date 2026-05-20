"""Tests for repository helpers — phone lookup, name+dob fuzzy match,
availability query, idempotent appointment creation, cancellation."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from prosper.ehr import repository as repo
from prosper.ehr.models import AppointmentStatus, Base, Patient, Provider, Slot


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    s = Session(engine)
    yield s
    s.close()


def _seed_provider(session: Session) -> Provider:
    p = Provider(name="Dr. Patel", timezone="America/New_York")
    session.add(p)
    session.commit()
    return p


def _seed_slots(session: Session, provider: Provider, count: int = 3) -> list[Slot]:
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    slots: list[Slot] = []
    for i in range(count):
        s = Slot(
            provider=provider,
            start_at=start + timedelta(minutes=30 * i),
            end_at=start + timedelta(minutes=30 * (i + 1)),
        )
        session.add(s)
        slots.append(s)
    session.commit()
    return slots


def test_find_patient_by_phone_returns_match(session: Session) -> None:
    session.add(
        Patient(
            first_name="Ada",
            last_name="Lovelace",
            name_normalized="ada lovelace",
            dob=date(1990, 12, 10),
            phone="+12025550100",
        )
    )
    session.commit()
    matches = repo.find_patient_by_phone(session, "+12025550100")
    assert len(matches) == 1
    assert matches[0].first_name == "Ada"


def test_find_patient_by_phone_returns_empty_on_miss(session: Session) -> None:
    assert repo.find_patient_by_phone(session, "+19999999999") == []


def test_find_patient_by_name_dob_fuzzy(session: Session) -> None:
    session.add(
        Patient(
            first_name="Ada",
            last_name="Lovelace",
            name_normalized="ada lovelace",
            dob=date(1990, 12, 10),
            phone="+12025550100",
        )
    )
    session.commit()
    results = repo.find_patient_by_name_dob(session, "Ada Lovelas", date(1990, 12, 10))
    assert len(results) == 1
    patient, similarity = results[0]
    assert patient.first_name == "Ada"
    assert similarity >= 0.85


def test_find_patient_by_name_dob_requires_dob_match(session: Session) -> None:
    session.add(
        Patient(
            first_name="Ada",
            last_name="Lovelace",
            name_normalized="ada lovelace",
            dob=date(1990, 12, 10),
            phone="+12025550100",
        )
    )
    session.commit()
    assert repo.find_patient_by_name_dob(session, "Ada Lovelace", date(1991, 1, 1)) == []


def test_create_patient_inserts_and_normalises(session: Session) -> None:
    p = repo.create_patient(
        session,
        first_name="José",
        last_name="Martí",
        dob=date(1853, 1, 28),
        phone="(202) 555-0199",
    )
    assert p.phone == "+12025550199"
    assert p.name_normalized == "jose marti"


def test_list_availability_excludes_booked_and_blocked(session: Session) -> None:
    provider = _seed_provider(session)
    slots = _seed_slots(session, provider, count=3)
    slots[1].is_blocked = True
    patient = repo.create_patient(
        session,
        first_name="A",
        last_name="B",
        dob=date(1990, 1, 1),
        phone="2025550111",
    )
    repo.create_appointment(session, patient_id=patient.id, slot_id=slots[0].id)
    available = repo.list_available_slots(session, date_=slots[0].start_at.date())
    available_ids = {s.id for s in available}
    assert slots[0].id not in available_ids
    assert slots[1].id not in available_ids
    assert slots[2].id in available_ids


def test_create_appointment_is_idempotent_for_same_patient(session: Session) -> None:
    provider = _seed_provider(session)
    [slot] = _seed_slots(session, provider, count=1)
    patient = repo.create_patient(
        session,
        first_name="A",
        last_name="B",
        dob=date(1990, 1, 1),
        phone="2025550111",
    )
    a1 = repo.create_appointment(session, patient_id=patient.id, slot_id=slot.id)
    a2 = repo.create_appointment(session, patient_id=patient.id, slot_id=slot.id)
    assert a1.id == a2.id


def test_create_appointment_conflict_for_other_patient(session: Session) -> None:
    provider = _seed_provider(session)
    [slot] = _seed_slots(session, provider, count=1)
    a = repo.create_patient(
        session,
        first_name="A",
        last_name="B",
        dob=date(1990, 1, 1),
        phone="2025550111",
    )
    b = repo.create_patient(
        session,
        first_name="C",
        last_name="D",
        dob=date(1991, 2, 2),
        phone="2025550122",
    )
    repo.create_appointment(session, patient_id=a.id, slot_id=slot.id)
    with pytest.raises(repo.SlotTakenError) as excinfo:
        repo.create_appointment(session, patient_id=b.id, slot_id=slot.id)
    assert excinfo.value.owner_patient_id == a.id


def test_cancel_appointment_marks_status_and_frees_slot(session: Session) -> None:
    provider = _seed_provider(session)
    [slot] = _seed_slots(session, provider, count=1)
    patient = repo.create_patient(
        session,
        first_name="A",
        last_name="B",
        dob=date(1990, 1, 1),
        phone="2025550111",
    )
    appt = repo.create_appointment(session, patient_id=patient.id, slot_id=slot.id)
    repo.cancel_appointment(session, appointment_id=appt.id, reason="test")
    session.refresh(appt)
    assert appt.status is AppointmentStatus.CANCELLED
    assert appt.cancelled_at is not None
    later = repo.create_appointment(session, patient_id=patient.id, slot_id=slot.id)
    assert later.id != appt.id


def test_list_availability_excludes_past_slots(session: Session) -> None:
    """Audit A1: never offer a slot whose start_at already passed."""
    provider = _seed_provider(session)
    today = datetime.now(timezone.utc).date()
    past = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    past_slot = Slot(provider=provider, start_at=past, end_at=past + timedelta(minutes=30))
    future_slot = Slot(provider=provider, start_at=future, end_at=future + timedelta(minutes=30))
    session.add_all([past_slot, future_slot])
    session.commit()
    available = repo.list_available_slots(session, date_=today)
    ids = {s.id for s in available}
    assert past_slot.id not in ids
    assert future_slot.id in ids


def test_get_upcoming_appointments_returns_only_scheduled_future(session: Session) -> None:
    provider = _seed_provider(session)
    slots = _seed_slots(session, provider, count=2)
    patient = repo.create_patient(
        session,
        first_name="A",
        last_name="B",
        dob=date(1990, 1, 1),
        phone="2025550111",
    )
    a1 = repo.create_appointment(session, patient_id=patient.id, slot_id=slots[0].id)
    a2 = repo.create_appointment(session, patient_id=patient.id, slot_id=slots[1].id)
    repo.cancel_appointment(session, appointment_id=a2.id, reason="x")
    upcoming = repo.get_upcoming_appointments(session, patient_id=patient.id)
    assert [a.id for a in upcoming] == [a1.id]


# ---------------------------------------------------------------------------
# Mutation-survivor regressions (manual mutmut pass — Windows lacks mutmut).
# These pin down boundary conditions that line-coverage missed:
#   - day-window upper bound (< day_end vs <= day_end)
#   - fuzzy-match similarity inclusivity (>= min_similarity vs > min_similarity)
# The "past slot" cutoff lower bound is exercised indirectly via the
# `list_available_slots` future-only filter; explicit boundary coverage lives
# in `test_list_availability_excludes_slot_at_or_after_midnight_next_day`.
# ---------------------------------------------------------------------------


def test_list_availability_excludes_slot_at_or_after_midnight_next_day(
    session: Session,
) -> None:
    """Mutation: ``Slot.start_at < day_end`` → ``<= day_end``.

    A slot whose start_at is exactly midnight of the *next* day must NOT be
    returned by the lookup for the previous day. This pins the strict ``<``
    boundary on the upper end of the day window.
    """
    provider = _seed_provider(session)
    target_day = (datetime.now(timezone.utc) + timedelta(days=2)).date()
    next_midnight = datetime.combine(
        target_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    )
    edge_slot = Slot(
        provider=provider,
        start_at=next_midnight,
        end_at=next_midnight + timedelta(minutes=30),
    )
    session.add(edge_slot)
    session.commit()
    available = repo.list_available_slots(session, date_=target_day)
    assert edge_slot.id not in {s.id for s in available}, (
        "slot at exactly day_end (next-day midnight) leaked into the previous-day query"
    )


def test_list_availability_includes_slot_exactly_at_day_start(session: Session) -> None:
    """Mutation: ``Slot.start_at >= cutoff`` → ``> cutoff``.

    For a future day the cutoff is ``max(day_start, now) == day_start``. A slot
    whose start_at is exactly midnight (== day_start) must be returned. This
    pins the inclusive lower boundary.
    """
    provider = _seed_provider(session)
    target_day = (datetime.now(timezone.utc) + timedelta(days=3)).date()
    day_start = datetime.combine(target_day, datetime.min.time(), tzinfo=timezone.utc)
    edge_slot = Slot(
        provider=provider,
        start_at=day_start,
        end_at=day_start + timedelta(minutes=30),
    )
    session.add(edge_slot)
    session.commit()
    available = repo.list_available_slots(session, date_=target_day)
    assert edge_slot.id in {s.id for s in available}, (
        "slot starting exactly at day_start (00:00 UTC) must be included"
    )


def test_find_patient_by_name_dob_includes_exact_threshold(session: Session) -> None:
    """Mutation: ``sim >= min_similarity`` → ``sim > min_similarity``.

    With an explicit ``min_similarity=1.0`` and a 100%-token-sort match, the
    candidate must be returned. This pins the inclusive ``>=`` boundary.
    """
    session.add(
        Patient(
            first_name="Ada",
            last_name="Lovelace",
            name_normalized="ada lovelace",
            dob=date(1990, 12, 10),
            phone="+12025550100",
        )
    )
    session.commit()
    results = repo.find_patient_by_name_dob(
        session, "Ada Lovelace", date(1990, 12, 10), min_similarity=1.0
    )
    assert len(results) == 1
    _, similarity = results[0]
    assert similarity == 1.0
