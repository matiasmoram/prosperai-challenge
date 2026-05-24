"""Adversarial: verify (not assume) the load-bearing EHR safety invariants.

These exercise the claims the repository docstrings make but that are easy to
silently break in a refactor: atomic reschedule (no orphan on conflict),
idempotent cancel (no double audit note), and multi-slot adjacency locking.
All are expected to PASS — they are regression guards on safety-critical paths.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from prosper.ehr import repository as repo
from prosper.ehr.db import get_engine, init_db
from prosper.ehr.models import Appointment, AppointmentStatus, Provider, Slot


@pytest.fixture
def repo_session(tmp_path, monkeypatch):
    """Temp SQLite EHR: one provider, three consecutive free 30-min slots,
    two registered patients. Yields (session, p1_id, p2_id, [slot_ids])."""
    monkeypatch.setenv("PROSPER_DB_URL", f"sqlite:///{tmp_path / 'ehr.db'}")
    engine = get_engine(reset=True)
    init_db()
    with Session(engine) as session:
        prov = Provider(name="Dr. Patel", timezone="UTC")
        session.add(prov)
        session.commit()
        start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        slots = []
        for i in range(3):
            sl = Slot(
                provider_id=prov.id,
                start_at=start + timedelta(minutes=30 * i),
                end_at=start + timedelta(minutes=30 * (i + 1)),
            )
            session.add(sl)
            slots.append(sl)
        session.commit()
        slot_ids = [s.id for s in slots]
        p1 = repo.create_patient(
            session, first_name="Jane", last_name="Doe", dob=date(1990, 1, 1), phone="5551230001"
        )
        p2 = repo.create_patient(
            session, first_name="John", last_name="Roe", dob=date(1985, 2, 2), phone="5551230002"
        )
        yield session, p1.id, p2.id, slot_ids


def test_reschedule_to_taken_slot_preserves_original(repo_session) -> None:
    """If the new slot is held by someone else, the original appointment must
    survive unchanged — no orphan, no lost booking (the atomic-reschedule
    contract)."""
    session, p1, p2, slots = repo_session
    appt = repo.create_appointment(session, patient_id=p1, slot_id=slots[0], duration_minutes=30)
    repo.create_appointment(session, patient_id=p2, slot_id=slots[1], duration_minutes=30)

    with pytest.raises(repo.SlotTakenError):
        repo.reschedule_appointment(session, appointment_id=appt.id, new_slot_id=slots[1])

    session.expire_all()
    survived = session.get(Appointment, appt.id)
    assert survived is not None
    assert survived.status == AppointmentStatus.SCHEDULED
    assert survived.slot_id == slots[0], "original slot must be preserved after failed reschedule"


def test_cancel_is_idempotent(repo_session) -> None:
    """Cancelling twice is a no-op the second time: status stays cancelled,
    cancelled_at and the [cancel] audit note are not duplicated."""
    session, p1, _p2, slots = repo_session
    appt = repo.create_appointment(session, patient_id=p1, slot_id=slots[0], duration_minutes=30)
    repo.cancel_appointment(session, appointment_id=appt.id, reason="changed plans")
    session.expire_all()
    first = session.get(Appointment, appt.id)
    first_cancelled_at = first.cancelled_at
    first_notes = first.notes

    repo.cancel_appointment(session, appointment_id=appt.id, reason="changed plans again")
    session.expire_all()
    second = session.get(Appointment, appt.id)
    assert second.status == AppointmentStatus.CANCELLED
    assert second.cancelled_at == first_cancelled_at
    assert second.notes == first_notes  # second reason not appended
    assert (second.notes or "").count("[cancel]") == 1


def test_60min_booking_locks_adjacent_slot(repo_session) -> None:
    """A 60-min booking locks the anchor + next slot; another patient must not
    be able to book that adjacent slot."""
    session, p1, p2, slots = repo_session
    repo.create_appointment(session, patient_id=p1, slot_id=slots[0], duration_minutes=60)
    with pytest.raises(repo.SlotTakenError):
        repo.create_appointment(session, patient_id=p2, slot_id=slots[1], duration_minutes=30)


def test_cancel_frees_slot_for_rebooking(repo_session) -> None:
    """After cancelling, the freed slot can be booked by a different patient."""
    session, p1, p2, slots = repo_session
    appt = repo.create_appointment(session, patient_id=p1, slot_id=slots[0], duration_minutes=30)
    repo.cancel_appointment(session, appointment_id=appt.id)
    rebooked = repo.create_appointment(
        session, patient_id=p2, slot_id=slots[0], duration_minutes=30
    )
    assert rebooked.patient_id == p2
    assert rebooked.status == AppointmentStatus.SCHEDULED
