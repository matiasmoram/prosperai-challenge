"""Seed the local SQLite EHR with demo data.

Inserts five providers across five specialties (Therapist, Psychiatrist,
General Practice, Dermatologist, Physiotherapist) and ~980 slots (next 14
days, 9am-5pm UTC, every 30 min, lunch noon-1pm skipped) plus 10 demo
patients. Idempotent: skips inserts if matching rows already exist.

Run: ``uv run python scripts/seed.py``
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from prosper.ehr.db import get_engine, init_db
from prosper.ehr.models import Patient, Provider, Slot
from prosper.ehr.repository import normalize_name

PROVIDERS = [
    ("Dr. Aisha Patel", "America/New_York", "Therapist"),
    ("Dr. Marcus Chen", "America/New_York", "Psychiatrist"),
    ("Dr. Sofia Romero", "America/New_York", "General Practice"),
    ("Dr. Liam Okonkwo", "America/New_York", "Dermatologist"),
    ("Dr. Yuki Tanaka", "America/New_York", "Physiotherapist"),
]

def _patient(first: str, last: str, dob: date, phone: str) -> dict[str, object]:
    return {
        "first_name": first,
        "last_name": last,
        # Use the full normalisation pipeline (diacritics, honorifics, bidi
        # marks) rather than a bare .lower() so the stored value matches what
        # repo.find_patient_by_name_dob compares against at lookup time.
        "name_normalized": normalize_name(f"{first} {last}"),
        "dob": dob,
        "phone": phone,
    }


# A fuller roster so the demo DB looks like a real clinic (10 patients).
# Phones are distinct +1-202-555-01xx test numbers (NANP 555 reserved range).
DEMO_PATIENTS = [
    _patient("Ada", "Lovelace", date(1990, 12, 10), "+12025550100"),
    _patient("Grace", "Hopper", date(1906, 12, 9), "+12025550111"),
    _patient("John", "Smith", date(1985, 3, 12), "+12025550102"),
    _patient("Maria", "Garcia", date(1972, 11, 4), "+12025550103"),
    _patient("James", "Williams", date(1968, 7, 21), "+12025550104"),
    _patient("Patricia", "Brown", date(1995, 2, 28), "+12025550105"),
    _patient("Robert", "Jones", date(1959, 9, 15), "+12025550106"),
    _patient("Linda", "Nguyen", date(2001, 6, 3), "+12025550107"),
    _patient("Michael", "O'Brien", date(1980, 1, 30), "+12025550108"),
    _patient("Fatima", "Al-Sayed", date(1993, 10, 18), "+12025550109"),
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
            f"seeded: providers={len(prov_count)} slots={len(slot_count)} patients={len(pat_count)}"
        )


if __name__ == "__main__":
    main()
