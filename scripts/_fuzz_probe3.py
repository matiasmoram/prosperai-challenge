"""Use TestClient against fresh app to validate Pydantic limits + nested JSON."""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from prosper.ehr.api import create_app
from prosper.ehr.db import make_session_factory
from prosper.ehr.models import Provider, Slot


def main() -> None:
    import os, tempfile
    db_path = os.path.join(tempfile.gettempdir(), "fuzz_probe.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    eng = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    app = create_app(engine=eng)
    client = TestClient(app)

    print("--- Topic 4: Massive payload ---")
    big = "A" * 1_000_000
    r = client.post(
        "/patients",
        json={"first_name": big, "last_name": "X", "dob": "1990-01-01", "phone": "+15551110001"},
    )
    print(f"1M first_name: status={r.status_code}, errs={str(r.json())[:200]}")

    big_phone = "+" + "1" * 100000
    r = client.post(
        "/patients",
        json={"first_name": "Bob", "last_name": "X", "dob": "1990-01-01", "phone": big_phone},
    )
    print(f"Huge phone: status={r.status_code}, errs={str(r.json())[:200]}")

    # Deeply nested JSON in notes — body itself accepts only str, but caller may post other shape
    r = client.post(
        "/patients",
        json={
            "first_name": "Bob",
            "last_name": "X",
            "dob": "1990-01-01",
            "phone": "+15551110002",
            "email": "a" * 1000,
        },
    )
    print(f"Huge email: status={r.status_code}, errs={str(r.json())[:200]}")

    print()
    print("--- Topic 8: get upcoming for unknown patient_id ---")
    r = client.get("/patients/nonsense-id/appointments")
    print(f"unknown patient: status={r.status_code} body={r.text[:200]}")

    print()
    print("--- Topic 5: slot at start_at == now ---")
    # Insert provider + slot with start_at = now, then list
    Sess = make_session_factory(eng)
    with Sess() as s:
        prov = Provider(name="Dr. Now", timezone="UTC")
        s.add(prov)
        s.commit()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        slot_now = Slot(provider_id=prov.id, start_at=now, end_at=now + timedelta(minutes=30))
        slot_past = Slot(
            provider_id=prov.id,
            start_at=now - timedelta(seconds=1),
            end_at=now + timedelta(minutes=29),
        )
        slot_future = Slot(
            provider_id=prov.id,
            start_at=now + timedelta(minutes=1),
            end_at=now + timedelta(minutes=31),
        )
        s.add_all([slot_now, slot_past, slot_future])
        s.commit()
        d = now.date().isoformat()
        r = client.get(f"/availability?date={d}")
        slots = r.json().get("slots", [])
        print(f"slot-at-now visible: {any(sl['id']==slot_now.id for sl in slots)}")
        print(f"slot-past visible: {any(sl['id']==slot_past.id for sl in slots)}")
        print(f"slot-future visible: {any(sl['id']==slot_future.id for sl in slots)}")

    print()
    print("--- Topic 7: cancel an already-cancelled appointment ---")
    # Create patient, slot, appt
    with Sess() as s:
        prov = s.query(Provider).first()
        future_slot = Slot(
            provider_id=prov.id,
            start_at=now + timedelta(days=1),
            end_at=now + timedelta(days=1, minutes=30),
        )
        s.add(future_slot)
        s.commit()
        future_slot_id = future_slot.id
    r = client.post(
        "/patients",
        json={"first_name": "Cx", "last_name": "Y", "dob": "1990-01-01", "phone": "+15552220001"},
    )
    pid = r.json()["id"]
    r = client.post("/appointments", json={"patient_id": pid, "slot_id": future_slot_id})
    aid = r.json()["id"]
    r1 = client.post(f"/appointments/{aid}/cancel", json={"reason": "first"})
    r2 = client.post(f"/appointments/{aid}/cancel", json={"reason": "second"})
    print(f"first cancel: {r1.status_code} {r1.json().get('status')}")
    print(f"second cancel: {r2.status_code} {r2.json().get('status')} notes={r2.json().get('notes')!r}")

    print()
    print("--- Topic 6: cancelled doesn't count — rebook after cancel ---")
    with Sess() as s:
        prov = s.query(Provider).first()
        slot2 = Slot(
            provider_id=prov.id,
            start_at=now + timedelta(days=2),
            end_at=now + timedelta(days=2, minutes=30),
        )
        s.add(slot2)
        s.commit()
        slot2_id = slot2.id
    a = client.post("/appointments", json={"patient_id": pid, "slot_id": slot2_id})
    aid2 = a.json()["id"]
    client.post(f"/appointments/{aid2}/cancel", json={"reason": "oops"})
    b = client.post("/appointments", json={"patient_id": pid, "slot_id": slot2_id})
    print(f"rebook after cancel: status={b.status_code} id_new={b.json().get('id')} (was {aid2})")


if __name__ == "__main__":
    main()
