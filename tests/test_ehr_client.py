"""Tests for the httpx-based async EHR client. Uses ASGITransport so the
client talks to the FastAPI app in-process without a separate uvicorn.

The ``asgi_client`` fixture is hoisted into ``tests/conftest.py`` as
``seeded_ehr_client`` and re-exported under the legacy name.
"""

from datetime import date, datetime, timedelta, timezone

from prosper.ehr_client import EHRClient


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

        cancelled = await asgi_client.cancel_appointment(appointment_id=appt["id"], reason="t")
        assert cancelled["status"] == "cancelled"
