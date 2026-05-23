"""Adversarial: same-patient slot re-booking is misreported as taken by another.

Finding F-005. `repo.create_appointment` only short-circuits as idempotent
when BOTH the patient_id AND the duration match the existing scheduled
appointment. A patient who already holds the anchor slot for 30 min and then
tries to extend to 60 min on the same anchor (with the adjacent block free)
gets a `SlotTakenError` whose `owner_patient_id` is *themselves*. The tools
layer blindly maps any 409 slot_taken to `slot_taken_other_patient`, so the
caller is told their own slot is "taken by another patient".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from prosper.ehr import repository as repo
from prosper.ehr.db import get_engine, init_db
from prosper.ehr.models import Provider, Slot


@pytest.fixture
def repo_session(tmp_path, monkeypatch):
    """A Session over a temp SQLite EHR: one provider, three consecutive
    free slots, one registered patient. Yields (session, patient_id, slot_ids).
    """
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
        patient = repo.create_patient(
            session,
            first_name="Jane",
            last_name="Doe",
            dob=date(1990, 1, 1),
            phone="5551234567",
        )
        yield session, patient.id, slot_ids


def test_same_patient_same_slot_same_duration_is_idempotent(repo_session) -> None:
    """Baseline: re-booking the identical appointment returns the same row."""
    session, patient_id, slot_ids = repo_session
    a1 = repo.create_appointment(
        session, patient_id=patient_id, slot_id=slot_ids[0], duration_minutes=30
    )
    a2 = repo.create_appointment(
        session, patient_id=patient_id, slot_id=slot_ids[0], duration_minutes=30
    )
    assert a1.id == a2.id


def test_self_collision_reports_self_as_owner(repo_session) -> None:
    """Documents the trap: the SlotTakenError owner is the caller themselves.

    This is why `tools.slot_taken_other_patient` is a lie in this path —
    the owner is not "another" patient. Plain (passing) test pinning the
    current behaviour so the F-005 fix has a clear before/after.
    """
    session, patient_id, slot_ids = repo_session
    repo.create_appointment(
        session, patient_id=patient_id, slot_id=slot_ids[0], duration_minutes=30
    )
    with pytest.raises(repo.SlotTakenError) as excinfo:
        repo.create_appointment(
            session, patient_id=patient_id, slot_id=slot_ids[0], duration_minutes=60
        )
    assert excinfo.value.owner_patient_id == patient_id  # the caller, not "another"


@pytest.mark.xfail(
    strict=True,
    reason="F-005: a patient extending their OWN booking on the same anchor "
    "to 60 min (adjacent block free) should succeed or raise a self-distinct "
    "error — never be reported as taken by another patient.",
)
def test_same_patient_can_extend_own_booking_on_free_chain(repo_session) -> None:
    """Extending your own 30-min booking to 60 min on a free chain should work."""
    session, patient_id, slot_ids = repo_session
    repo.create_appointment(
        session, patient_id=patient_id, slot_id=slot_ids[0], duration_minutes=30
    )
    # slot_ids[1] is free, so the 60-min chain (anchor + next) is satisfiable.
    appt = repo.create_appointment(
        session, patient_id=patient_id, slot_id=slot_ids[0], duration_minutes=60
    )
    assert appt.duration_minutes == 60
