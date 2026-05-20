"""End-to-end HTTP tests against the FastAPI EHR using TestClient."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from prosper.ehr.api import create_app
from prosper.ehr.db import Base, get_engine
from prosper.ehr.models import Provider, Slot


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("PROSPER_DB_URL", f"sqlite:///{tmp_path / 'ehr.db'}")
    engine = get_engine(reset=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        provider = Provider(name="Dr. Patel", timezone="America/New_York")
        session.add(provider)
        session.commit()
        start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        for i in range(3):
            session.add(
                Slot(
                    provider_id=provider.id,
                    start_at=start + timedelta(minutes=30 * i),
                    end_at=start + timedelta(minutes=30 * (i + 1)),
                )
            )
        session.commit()
    return TestClient(create_app())


def test_create_then_find_patient_by_phone(client: TestClient) -> None:
    r = client.post(
        "/patients",
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "dob": "1990-12-10",
            "phone": "(202) 555-0100",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["phone"] == "+12025550100"
    r2 = client.get("/patients/by-phone", params={"phone": "2025550100"})
    assert r2.status_code == 200
    assert len(r2.json()["patients"]) == 1


def test_find_patient_by_name_dob_returns_similarity(client: TestClient) -> None:
    client.post(
        "/patients",
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "dob": "1990-12-10",
            "phone": "2025550100",
        },
    )
    r = client.get(
        "/patients/by-name-dob",
        params={"name": "Ada Lovelas", "dob": "1990-12-10"},
    )
    assert r.status_code == 200
    patients = r.json()["patients"]
    assert len(patients) == 1
    assert patients[0]["similarity"] >= 0.85


def test_availability_lists_seed_slots(client: TestClient) -> None:
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    r = client.get("/availability", params={"date": tomorrow})
    assert r.status_code == 200
    assert len(r.json()["slots"]) == 3


def test_book_then_cancel_appointment_roundtrip(client: TestClient) -> None:
    p = client.post(
        "/patients",
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "dob": "1990-12-10",
            "phone": "2025550100",
        },
    ).json()
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    slot = client.get("/availability", params={"date": tomorrow}).json()["slots"][0]
    book = client.post("/appointments", json={"patient_id": p["id"], "slot_id": slot["id"]})
    assert book.status_code == 201, book.text
    appt = book.json()
    assert appt["status"] == "scheduled"
    book2 = client.post("/appointments", json={"patient_id": p["id"], "slot_id": slot["id"]})
    assert book2.status_code in (200, 201)
    assert book2.json()["id"] == appt["id"]
    c = client.post(f"/appointments/{appt['id']}/cancel", json={"reason": "test"})
    assert c.status_code == 200
    assert c.json()["status"] == "cancelled"


def test_book_other_patient_on_taken_slot_returns_409(client: TestClient) -> None:
    p1 = client.post(
        "/patients",
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "dob": "1990-12-10",
            "phone": "2025550100",
        },
    ).json()
    p2 = client.post(
        "/patients",
        json={
            "first_name": "Grace",
            "last_name": "Hopper",
            "dob": "1906-12-09",
            "phone": "2025550111",
        },
    ).json()
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    slot = client.get("/availability", params={"date": tomorrow}).json()["slots"][0]
    client.post("/appointments", json={"patient_id": p1["id"], "slot_id": slot["id"]})
    r = client.post("/appointments", json={"patient_id": p2["id"], "slot_id": slot["id"]})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "slot_taken"


def test_get_patient_appointments(client: TestClient) -> None:
    p = client.post(
        "/patients",
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "dob": "1990-12-10",
            "phone": "2025550100",
        },
    ).json()
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    slot = client.get("/availability", params={"date": tomorrow}).json()["slots"][0]
    client.post("/appointments", json={"patient_id": p["id"], "slot_id": slot["id"]})
    r = client.get(f"/patients/{p['id']}/appointments")
    assert r.status_code == 200
    assert len(r.json()["appointments"]) == 1
