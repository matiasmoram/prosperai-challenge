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
    return TestClient(create_app(engine=engine))


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


def test_create_patient_duplicate_phone_different_format_returns_409(
    client: TestClient,
) -> None:
    """Same phone in raw + E.164 form must collide as 409, never 500.

    Regression for the bypassed duplicate-phone guard: when the API-side
    lookup and the repo-side insert disagreed on normalisation, the
    second POST escaped the 409 ``patient_exists`` branch and tripped
    the DB ``UNIQUE`` constraint as a 500 ``IntegrityError``.
    """
    r1 = client.post(
        "/patients",
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "dob": "1990-12-10",
            "phone": "2025551234",
        },
    )
    assert r1.status_code == 201, r1.text
    first_id = r1.json()["id"]
    r2 = client.post(
        "/patients",
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "dob": "1990-12-10",
            "phone": "+12025551234",
        },
    )
    assert r2.status_code == 409, r2.text
    body = r2.json()
    assert body["detail"]["code"] == "patient_exists"
    assert body["detail"]["id"] == first_id


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


def test_metrics_endpoint_exposes_counts_in_prom_text_format(client: TestClient) -> None:
    """/metrics is a hand-rolled Prometheus text endpoint — no extra deps."""
    client.post(
        "/patients",
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "dob": "1990-12-10",
            "phone": "2025550100",
        },
    )
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    # Required series names (gauges + counter).
    assert "ehr_patients_total 1" in body
    assert "ehr_slots_total 3" in body  # 3 seeded by the fixture
    assert 'ehr_appointments_total{status="scheduled"} 0' in body
    assert 'ehr_appointments_total{status="cancelled"} 0' in body
    # /metrics itself is counted (this is the first hit).
    assert 'http_requests_total{path="/metrics"} 1' in body
    # Prom text format requires HELP + TYPE comment lines.
    assert "# HELP ehr_patients_total" in body
    assert "# TYPE ehr_patients_total gauge" in body


def test_x_request_id_is_echoed_in_response_headers(client: TestClient) -> None:
    """Bot sends X-Request-Id; EHR echoes it so grep lines up on both sides."""
    r = client.get("/health", headers={"X-Request-Id": "sess-abc-1-1"})
    assert r.status_code == 200
    assert r.headers["X-Request-Id"] == "sess-abc-1-1"


# ---------------------------------------------------------------------------
# Variable visit duration over HTTP (ADR 005). Fixture seeds 3 consecutive
# slots (9:00, 9:30, 10:00) under one General Practice provider.
# ---------------------------------------------------------------------------


def _book_phone_patient(client: TestClient) -> str:
    r = client.post(
        "/patients",
        json={
            "first_name": "Dur",
            "last_name": "Ation",
            "dob": "1990-01-01",
            "phone": "2025559000",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_availability_duration_60_returns_only_valid_anchors(client: TestClient) -> None:
    day = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    r = client.get("/availability", params={"date": day, "duration_minutes": 60})
    assert r.status_code == 200, r.text
    # 9:00 (→9:30 free) and 9:30 (→10:00 free) qualify; 10:00 has no 10:30.
    assert len(r.json()["slots"]) == 2


def test_availability_duration_90_returns_single_anchor(client: TestClient) -> None:
    day = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    r = client.get("/availability", params={"date": day, "duration_minutes": 90})
    assert r.status_code == 200, r.text
    assert len(r.json()["slots"]) == 1  # only 9:00 has two successors


def test_availability_invalid_duration_returns_400(client: TestClient) -> None:
    day = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    r = client.get("/availability", params={"date": day, "duration_minutes": 45})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "invalid_duration"


def test_create_60min_appointment_locks_two_slots(client: TestClient) -> None:
    patient_id = _book_phone_patient(client)
    day = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    slots = client.get("/availability", params={"date": day, "duration_minutes": 60}).json()[
        "slots"
    ]
    anchor = slots[0]["id"]
    r = client.post(
        "/appointments",
        json={"patient_id": patient_id, "slot_id": anchor, "duration_minutes": 60},
    )
    assert r.status_code == 201, r.text
    assert r.json()["duration_minutes"] == 60
    # 9:00 + 9:30 now locked → a 30-min search sees only 10:00.
    left = client.get("/availability", params={"date": day, "duration_minutes": 30}).json()["slots"]
    assert len(left) == 1


def test_create_60min_on_last_slot_returns_409_no_consecutive(client: TestClient) -> None:
    patient_id = _book_phone_patient(client)
    day = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    all30 = client.get("/availability", params={"date": day, "duration_minutes": 30}).json()[
        "slots"
    ]
    last_anchor = all30[-1]["id"]  # 10:00 — no 10:30 after it
    r = client.post(
        "/appointments",
        json={"patient_id": patient_id, "slot_id": last_anchor, "duration_minutes": 60},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "no_consecutive_slots"


def test_create_invalid_duration_returns_400(client: TestClient) -> None:
    patient_id = _book_phone_patient(client)
    day = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    anchor = client.get("/availability", params={"date": day}).json()["slots"][0]["id"]
    r = client.post(
        "/appointments",
        json={"patient_id": patient_id, "slot_id": anchor, "duration_minutes": 45},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "invalid_duration"


def test_default_duration_is_30_and_single_slot(client: TestClient) -> None:
    patient_id = _book_phone_patient(client)
    day = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    anchor = client.get("/availability", params={"date": day}).json()["slots"][0]["id"]
    r = client.post("/appointments", json={"patient_id": patient_id, "slot_id": anchor})
    assert r.status_code == 201, r.text
    assert r.json()["duration_minutes"] == 30
    # Only the anchor consumed; the other two 30-min slots remain.
    left = client.get("/availability", params={"date": day}).json()["slots"]
    assert len(left) == 2


# ---------------------------------------------------------------------------
# Result-contract: malformed input never escapes as a 500 (council item 7).
# ---------------------------------------------------------------------------


def test_no_endpoint_returns_500_on_malformed_input(client: TestClient) -> None:
    """Every public endpoint must answer malformed/edge input with a typed
    4xx (validation 422, business 400/404/409) — never an uncaught 500."""
    day = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    calls = [
        # Pydantic validation failures → 422.
        ("POST", "/patients", {"json": {"first_name": "A"}}),  # missing required
        (
            "POST",
            "/patients",
            {
                "json": {
                    "first_name": "A",
                    "last_name": "B",
                    "dob": "1990-01-01",
                    "phone": "abc<script>",
                }
            },
        ),  # hostile phone
        # Bad query params.
        ("GET", "/patients/by-phone", {"params": {"phone": "x"}}),  # min_length
        ("GET", "/availability", {"params": {"date": "not-a-date"}}),  # bad date
        ("GET", "/availability", {"params": {"date": day, "duration_minutes": 45}}),  # invalid dur
        # Nonexistent ids.
        (
            "POST",
            "/appointments",
            {"json": {"patient_id": "nope", "slot_id": "nope", "duration_minutes": 30}},
        ),
        ("POST", "/appointments/ghost/cancel", {"json": {"reason": "x"}}),
        ("PATCH", "/appointments/ghost", {"json": {"new_slot_id": "ghost"}}),
        ("GET", "/patients/ghost/appointments", {}),
    ]
    for method, path, kw in calls:
        r = client.request(method, path, **kw)
        assert r.status_code < 500, f"{method} {path} -> {r.status_code}: {r.text}"
        assert r.status_code >= 400, f"{method} {path} unexpectedly OK: {r.text}"
