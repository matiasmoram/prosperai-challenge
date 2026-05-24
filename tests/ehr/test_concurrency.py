"""Concurrency stress for the multi-slot booking lock (ADR 005).

The DB-level guarantee is the PK on ``appointment_slot_locks.slot_id``: a slot
can be held by at most one scheduled appointment. These tests hammer the same
anchor (and overlapping chains) from many real threads over a WAL-mode file
SQLite, asserting exactly one winner and zero orphaned locks — the property
that makes ``create_appointment`` safe without an application-level lock.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from prosper.ehr import repository as repo
from prosper.ehr.models import (
    Appointment,
    AppointmentSlotLock,
    AppointmentStatus,
    Base,
    Patient,
    Provider,
    Slot,
)


@pytest.fixture
def engine(tmp_path):
    """File-backed SQLite with WAL so multiple threads can write concurrently.

    ``:memory:`` is per-connection and would defeat a multi-session race, so
    we use a real file and mirror the production PRAGMAs from ``ehr.db``.
    """
    eng = create_engine(f"sqlite:///{tmp_path / 'race.db'}", future=True)

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    Base.metadata.create_all(eng)
    return eng


def _seed(engine, *, slot_count: int, patient_count: int) -> tuple[list[str], list[str]]:
    with Session(engine) as s:
        prov = Provider(name="Dr. Patel", timezone="UTC", specialty="General Practice")
        s.add(prov)
        s.commit()
        start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        slots = []
        for i in range(slot_count):
            sl = Slot(
                provider_id=prov.id,
                start_at=start + timedelta(minutes=30 * i),
                end_at=start + timedelta(minutes=30 * (i + 1)),
            )
            s.add(sl)
            slots.append(sl)
        s.commit()
        slot_ids = [sl.id for sl in slots]
        patient_ids = []
        for i in range(patient_count):
            p = Patient(
                first_name=f"P{i}",
                last_name="Racer",
                name_normalized=f"p{i} racer",
                dob=date(1990, 1, 1),
                phone=f"+1202555{i:04d}",
            )
            s.add(p)
            s.commit()
            patient_ids.append(p.id)
    return slot_ids, patient_ids


def _count_orphan_locks(engine) -> int:
    """Lock rows whose appointment is not SCHEDULED — should always be zero."""
    with Session(engine) as s:
        locks = s.execute(select(AppointmentSlotLock)).scalars().all()
        orphans = 0
        for lk in locks:
            appt = s.get(Appointment, lk.appointment_id)
            if appt is None or appt.status is not AppointmentStatus.SCHEDULED:
                orphans += 1
        return orphans


def test_20_racers_one_anchor_exactly_one_wins(engine) -> None:
    slot_ids, patient_ids = _seed(engine, slot_count=3, patient_count=20)
    anchor = slot_ids[0]
    factory = sessionmaker(engine)

    def _book(patient_id: str) -> str:
        with factory() as s:
            try:
                repo.create_appointment(
                    s, patient_id=patient_id, slot_id=anchor, duration_minutes=30
                )
                return "ok"
            except repo.SlotTakenError:
                return "slot_taken"

    with ThreadPoolExecutor(max_workers=20) as ex:
        outcomes = list(ex.map(_book, patient_ids))

    assert outcomes.count("ok") == 1, outcomes
    assert outcomes.count("slot_taken") == 19, outcomes
    assert _count_orphan_locks(engine) == 0
    # Exactly one scheduled appointment on the anchor.
    with Session(engine) as s:
        scheduled = (
            s.execute(
                select(Appointment)
                .where(Appointment.slot_id == anchor)
                .where(Appointment.status == AppointmentStatus.SCHEDULED)
            )
            .scalars()
            .all()
        )
        assert len(scheduled) == 1


def test_overlapping_60min_chains_one_winner(engine) -> None:
    """Two 60-min bookings whose chains overlap (anchor 9:00 vs 9:30, both need
    9:30) — at most one can win; the loser gets a clean error, no orphan lock."""
    slot_ids, patient_ids = _seed(engine, slot_count=3, patient_count=2)
    factory = sessionmaker(engine)
    # patient0 anchors at 9:00 (locks 9:00+9:30); patient1 anchors at 9:30
    # (locks 9:30+10:00). They collide on 9:30.
    plan = [(patient_ids[0], slot_ids[0]), (patient_ids[1], slot_ids[1])]

    def _book(arg: tuple[str, str]) -> str:
        patient_id, anchor = arg
        with factory() as s:
            try:
                repo.create_appointment(
                    s, patient_id=patient_id, slot_id=anchor, duration_minutes=60
                )
                return "ok"
            except (repo.SlotTakenError, repo.NoConsecutiveSlotsError):
                return "blocked"

    with ThreadPoolExecutor(max_workers=2) as ex:
        outcomes = list(ex.map(_book, plan))

    assert outcomes.count("ok") >= 1
    # The shared slot 9:30 can back only one appointment.
    with Session(engine) as s:
        locks_on_shared = (
            s.execute(select(AppointmentSlotLock).where(AppointmentSlotLock.slot_id == slot_ids[1]))
            .scalars()
            .all()
        )
        assert len(locks_on_shared) == 1
    assert _count_orphan_locks(engine) == 0
