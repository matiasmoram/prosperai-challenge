"""Seed the local SQLite EHR with demo data.

Inserts three providers and ~120 slots (next 14 days, 9am–5pm UTC, every 30
min, lunch noon-1pm skipped) plus 2 demo patients. Idempotent: skips inserts
if matching rows already exist.

Run: ``uv run python scripts/seed.py``
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from prosper.ehr.db import get_engine, init_db
from prosper.ehr.models import Patient, Provider, Slot

PROVIDERS = [
    ("Dr. Aisha Patel", "America/New_York", "Therapist"),
    ("Dr. Marcus Chen", "America/New_York", "Psychiatrist"),
    ("Dr. Sofia Romero", "America/New_York", "General Practice"),
    ("Dr. Liam Okonkwo", "America/New_York", "Dermatologist"),
    ("Dr. Yuki Tanaka", "America/New_York", "Physiotherapist"),
]

DEMO_PATIENTS = [
    {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "name_normalized": "ada lovelace",
        "dob": date(1990, 12, 10),
        "phone": "+12025550100",
    },
    {
        "first_name": "Grace",
        "last_name": "Hopper",
        "name_normalized": "grace hopper",
        "dob": date(1906, 12, 9),
        "phone": "+12025550111",
    },
]


def _make_slots_for_provider(provider_id: str, base: date) -> list[Slot]:
    slots: list[Slot] = []
    for day_offset in range(14):
        d = base + timedelta(days=day_offset)
        for hour in range(9, 17):
            if hour == 12:
                continue
            for minute in (0, 30):
                start = datetime.combine(d, time(hour, minute), tzinfo=timezone.utc)
                slots.append(
                    Slot(
                        provider_id=provider_id,
                        start_at=start,
                        end_at=start + timedelta(minutes=30),
                    )
                )
    return slots


def main() -> None:
    engine = get_engine()
    init_db()
    with Session(engine) as session:
        for name, tz, specialty in PROVIDERS:
            if not session.execute(select(Provider).where(Provider.name == name)).first():
                session.add(Provider(name=name, timezone=tz, specialty=specialty))
        session.commit()

        if session.execute(select(Slot).limit(1)).first() is None:
            base = datetime.now(timezone.utc).date() + timedelta(days=1)
            for prov in session.execute(select(Provider)).scalars():
                session.add_all(_make_slots_for_provider(prov.id, base))
            session.commit()

        for p in DEMO_PATIENTS:
            if not session.execute(select(Patient).where(Patient.phone == p["phone"])).first():
                session.add(Patient(**p))
        session.commit()

        prov_count = session.execute(select(Provider)).scalars().all()
        slot_count = session.execute(select(Slot)).scalars().all()
        pat_count = session.execute(select(Patient)).scalars().all()
        print(
            f"seeded: providers={len(prov_count)} slots={len(slot_count)} "
            f"patients={len(pat_count)}"
        )


if __name__ == "__main__":
    main()
