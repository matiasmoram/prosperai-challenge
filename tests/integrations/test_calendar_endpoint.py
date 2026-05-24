"""Integration tests for GET /appointments (clinic-wide calendar endpoint).

Uses the in-memory-engine + create_app(engine=) pattern for hermetic,
socket-free test isolation (same as tests/ehr/test_api.py fixtures).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from prosper.ehr.api import create_app
from prosper.ehr.db import init_db
from prosper.ehr.models import (
    Appointment,
    AppointmentStatus,
    Patient,
    Provider,
    Slot,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _memory_engine():  # type: ignore[no-untyped-def]
    """In-memory SQLite engine with a single shared connection (StaticPool).

    StaticPool forces every checkout to reuse the same underlying connection
    so that DDL run by ``init_db``/``create_app`` and the session used to
    seed data share the same in-memory database.  Without it, each
    ``engine.connect()`` call gets a brand-new `:memory:` database and the
    tables are invisible to the test session.
    """
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


@pytest.fixture()
def client() -> TestClient:
    """Fresh in-memory SQLite app per test — no shared state."""
    engine = _memory_engine()
    return TestClient(create_app(engine=engine))


@pytest.fixture()
def seeded_client() -> TestClient:
    """App with one provider, one patient, and two seeded appointments.

    Appointment A lands on ``TARGET_DATE``; appointment B lands the day after
    so out-of-range queries can be verified.
    """
    engine = _memory_engine()
    init_db(engine)

    with Session(engine) as session:
        provider = Provider(
            name="Dr. Test Provider",
            timezone="America/New_York",
            specialty="Therapist",
        )
        session.add(provider)
        session.flush()

        patient = Patient(
            first_name="Alice",
            last_name="Tester",
            name_normalized="alice tester",
            dob=date(1985, 6, 15),
            phone="+12025550199",
        )
        session.add(patient)
        session.flush()

        # Slot A: 2030-08-05 at 09:00 naive-UTC (far future — always > now())
        slot_a_start = datetime(2030, 8, 5, 9, 0)
        slot_a = Slot(
            provider_id=provider.id,
            start_at=slot_a_start,
            end_at=slot_a_start + timedelta(minutes=30),
        )
        session.add(slot_a)

        # Slot B: day after target at 09:00 — outside the single-day range
        slot_b_start = datetime(2030, 8, 6, 9, 0)
        slot_b = Slot(
            provider_id=provider.id,
            start_at=slot_b_start,
            end_at=slot_b_start + timedelta(minutes=30),
        )
        session.add(slot_b)
        session.flush()

        appt_a = Appointment(
            patient_id=patient.id,
            slot_id=slot_a.id,
            duration_minutes=30,
            status=AppointmentStatus.SCHEDULED,
            notes="Initial consult",
        )
        appt_b = Appointment(
            patient_id=patient.id,
            slot_id=slot_b.id,
            duration_minutes=30,
            status=AppointmentStatus.SCHEDULED,
            notes=None,
        )
        session.add_all([appt_a, appt_b])
        session.commit()

    return TestClient(create_app(engine=engine))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCalendarEndpoint:
    def test_in_range_returns_entry_with_patient_and_provider(
        self, seeded_client: TestClient
    ) -> None:
        """Appointment on 2030-08-05 appears when querying that single day."""
        r = seeded_client.get("/appointments", params={"from": "2030-08-05", "to": "2030-08-05"})
        assert r.status_code == 200
        data = r.json()
        assert "entries" in data
        assert len(data["entries"]) == 1
        entry = data["entries"][0]
        assert entry["patient_name"] == "Alice Tester"
        assert entry["provider_name"] == "Dr. Test Provider"
        assert entry["specialty"] == "Therapist"
        assert entry["duration_minutes"] == 30
        assert entry["status"] == "scheduled"
        assert entry["notes"] == "Initial consult"
        # start_at matches slot (naive-UTC serialised as ISO without tz offset)
        assert entry["start_at"].startswith("2030-08-05T09:00")

    def test_out_of_range_returns_empty(self, seeded_client: TestClient) -> None:
        """Query for a date with no appointments returns an empty entries list."""
        r = seeded_client.get("/appointments", params={"from": "2030-07-01", "to": "2030-07-31"})
        assert r.status_code == 200
        assert r.json()["entries"] == []

    def test_range_spanning_two_days_returns_both_entries(self, seeded_client: TestClient) -> None:
        """Inclusive to_date: querying 2030-08-05 to 2030-08-06 returns both appointments."""
        r = seeded_client.get("/appointments", params={"from": "2030-08-05", "to": "2030-08-06"})
        assert r.status_code == 200
        assert len(r.json()["entries"]) == 2

    def test_entries_ordered_by_start_at(self, seeded_client: TestClient) -> None:
        """Entries come back sorted earliest-first."""
        r = seeded_client.get("/appointments", params={"from": "2030-08-05", "to": "2030-08-06"})
        entries = r.json()["entries"]
        assert len(entries) == 2
        assert entries[0]["start_at"] < entries[1]["start_at"]

    def test_end_at_reflects_duration(self, seeded_client: TestClient) -> None:
        """end_at = start_at + duration_minutes (not just start_at + 30)."""
        r = seeded_client.get("/appointments", params={"from": "2030-08-05", "to": "2030-08-05"})
        entry = r.json()["entries"][0]
        start = datetime.fromisoformat(entry["start_at"])
        end = datetime.fromisoformat(entry["end_at"])
        assert (end - start).seconds // 60 == entry["duration_minutes"]

    def test_missing_from_param_returns_422(self, client: TestClient) -> None:
        """``from`` is required; omitting it yields 422 Unprocessable Entity."""
        r = client.get("/appointments", params={"to": "2030-08-05"})
        assert r.status_code == 422

    def test_missing_to_param_returns_422(self, client: TestClient) -> None:
        """``to`` is required; omitting it yields 422 Unprocessable Entity."""
        r = client.get("/appointments", params={"from": "2030-08-05"})
        assert r.status_code == 422

    def test_cancelled_appointments_excluded(self, client: TestClient) -> None:
        """Cancelled appointments do not appear in the calendar view."""
        engine = _memory_engine()
        init_db(engine)

        with Session(engine) as session:
            provider = Provider(
                name="Dr. Cancel Test", timezone="America/New_York", specialty="General Practice"
            )
            session.add(provider)
            session.flush()

            patient = Patient(
                first_name="Bob",
                last_name="Cancel",
                name_normalized="bob cancel",
                dob=date(1990, 1, 1),
                phone="+12025550198",
            )
            session.add(patient)
            session.flush()

            slot_start = datetime(2030, 9, 1, 10, 0)
            slot = Slot(
                provider_id=provider.id,
                start_at=slot_start,
                end_at=slot_start + timedelta(minutes=30),
            )
            session.add(slot)
            session.flush()

            appt = Appointment(
                patient_id=patient.id,
                slot_id=slot.id,
                duration_minutes=30,
                status=AppointmentStatus.CANCELLED,
            )
            session.add(appt)
            session.commit()

        tc = TestClient(create_app(engine=engine))
        r = tc.get("/appointments", params={"from": "2030-09-01", "to": "2030-09-01"})
        assert r.status_code == 200
        assert r.json()["entries"] == []

    def test_appointment_id_present_in_entry(self, seeded_client: TestClient) -> None:
        """Each entry carries a non-empty appointment_id string."""
        r = seeded_client.get("/appointments", params={"from": "2030-08-05", "to": "2030-08-05"})
        entry = r.json()["entries"][0]
        assert "appointment_id" in entry
        assert len(entry["appointment_id"]) > 0
