"""Tool handlers translate EHR client calls into Result[Ok,Err] for the LLM."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from prosper.ehr.api import create_app
from prosper.ehr.db import get_engine, init_db
from prosper.ehr.models import Provider, Slot
from prosper.ehr_client import EHRClient
from prosper.result import is_err, is_ok
from prosper.tools import (
    cancel_appointment_handler,
    create_appointment_handler,
    create_patient_handler,
    find_patient_by_name_dob_handler,
    find_patient_by_phone_handler,
    list_availability_slots_handler,
)


@pytest.fixture
def client(tmp_path, monkeypatch) -> EHRClient:
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


async def test_find_by_phone_no_match(client: EHRClient) -> None:
    async with client:
        r = await find_patient_by_phone_handler(client, phone="+19999999999")
    assert is_ok(r)
    assert r.value["found"] is False
    assert r.value["patients"] == []


async def test_create_then_find_then_book_then_cancel(client: EHRClient) -> None:
    async with client:
        created = await create_patient_handler(
            client,
            first_name="Ada",
            last_name="Lovelace",
            dob="1990-12-10",
            phone="2025550100",
        )
        assert is_ok(created)
        pid = created.value["patient_id"]

        slots_r = await list_availability_slots_handler(
            client,
            date=(datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat(),
        )
        assert is_ok(slots_r)
        slot_id = slots_r.value["slots"][0]["slot_id"]

        booked = await create_appointment_handler(client, patient_id=pid, slot_id=slot_id)
        assert is_ok(booked)
        appt_id = booked.value["appointment_id"]

        booked2 = await create_appointment_handler(client, patient_id=pid, slot_id=slot_id)
        assert is_ok(booked2) and booked2.value["appointment_id"] == appt_id

        cancelled = await cancel_appointment_handler(client, appointment_id=appt_id, reason="test")
        assert is_ok(cancelled)


async def test_book_same_slot_other_patient_returns_typed_err(client: EHRClient) -> None:
    async with client:
        a = (
            await create_patient_handler(
                client,
                first_name="A",
                last_name="A",
                dob="1990-01-01",
                phone="2025550100",
            )
        ).value["patient_id"]
        b = (
            await create_patient_handler(
                client,
                first_name="B",
                last_name="B",
                dob="1991-02-02",
                phone="2025550111",
            )
        ).value["patient_id"]
        slots = (
            await list_availability_slots_handler(
                client,
                date=(datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat(),
            )
        ).value["slots"]
        await create_appointment_handler(client, patient_id=a, slot_id=slots[0]["slot_id"])
        r = await create_appointment_handler(client, patient_id=b, slot_id=slots[0]["slot_id"])
    assert is_err(r)
    assert r.code == "slot_taken_other_patient"
    assert r.retryable is True


async def test_cancel_nonexistent_returns_typed_err(client: EHRClient) -> None:
    async with client:
        r = await cancel_appointment_handler(
            client, appointment_id="does-not-exist", reason=None
        )
    assert is_err(r)
    assert r.code == "appointment_not_found"


async def test_dob_parser_accepts_spoken_forms(client: EHRClient) -> None:
    async with client:
        r = await find_patient_by_name_dob_handler(
            client, name="ada lovelace", dob="December 10, 1990"
        )
    assert is_ok(r)
