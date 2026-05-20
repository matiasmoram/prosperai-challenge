"""Smoke tests for EHR ORM models."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from prosper.ehr.models import (
    Appointment,
    AppointmentStatus,
    Base,
    Patient,
    Provider,
    Slot,
)


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_can_create_provider_patient_slot_and_appointment() -> None:
    session = _make_session()
    provider = Provider(name="Dr. Patel", timezone="America/New_York")
    patient = Patient(
        first_name="Ada",
        last_name="Lovelace",
        name_normalized="ada lovelace",
        dob=date(1990, 12, 10),
        phone="+12025550100",
    )
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    slot = Slot(provider=provider, start_at=start, end_at=start + timedelta(minutes=30))
    appt = Appointment(patient=patient, slot=slot, status=AppointmentStatus.SCHEDULED)
    session.add_all([provider, patient, slot, appt])
    session.commit()

    loaded = session.execute(select(Appointment)).scalar_one()
    assert loaded.patient.first_name == "Ada"
    assert loaded.slot.provider.name == "Dr. Patel"
    assert loaded.status is AppointmentStatus.SCHEDULED
