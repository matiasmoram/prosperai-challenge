"""Tests for the httpx-based async EHR client. Uses ASGITransport so the
client talks to the FastAPI app in-process without a separate uvicorn."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from prosper.ehr.api import create_app
from prosper.ehr.db import get_engine, init_db
from prosper.ehr.models import Provider, Slot
from prosper.ehr_client import EHRClient


@pytest.fixture
def asgi_client(tmp_path, monkeypatch) -> EHRClient:
    monkeypatch.setenv("PROSPER_DB_URL", f"sqlite:///{tmp_path/'ehr.db'}")
    get_engine(reset=True)
    init_db()
    app = create_app()
    with Session(get_engine()) as session:
        prov = Provider(name="Dr. Patel", timezone="UTC")
        session.add(prov)
        session.commit()
        start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        for i in range(2):
            session.add(
                Slot(
                    provider_id=prov.id,
                    start_at=start + timedelta(minutes=30 * i),
                    end_at=start + timedelta(minutes=30 * (i + 1)),
                )
            )
        session.commit()
    return EHRClient.for_asgi_app(app)


async def test_create_find_book_cancel_roundtrip(asgi_client: EHRClient) -> None:
    async with asgi_client:
        created = await asgi_client.create_patient(
            first_name="Ada",
            last_name="Lovelace",
            dob=date(1990, 12, 10),
            phone="2025550100",
        )
        assert created["phone"] == "+12025550100"

        by_phone = await asgi_client.find_patients_by_phone("2025550100")
        assert len(by_phone) == 1

        slots = await asgi_client.list_availability(
            date_=(datetime.now(timezone.utc) + timedelta(days=1)).date()
        )
        assert len(slots) == 2

        appt = await asgi_client.create_appointment(
            patient_id=created["id"], slot_id=slots[0]["id"]
        )
        assert appt["status"] == "scheduled"

        cancelled = await asgi_client.cancel_appointment(
            appointment_id=appt["id"], reason="t"
        )
        assert cancelled["status"] == "cancelled"
