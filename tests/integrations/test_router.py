"""/frontdesk router: mail list + calendar proxy + SPA serve."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from prosper.integrations.mail import MailStore, make_message
from prosper.integrations.router import build_frontdesk_router


async def _fake_calendar(from_date: date, to_date: date) -> list[dict[str, Any]]:
    return [
        {
            "appointment_id": "a1",
            "patient_name": "Jane Doe",
            "provider_name": "Dr. Patel",
            "specialty": "Therapist",
            "start_at": "2026-05-26T14:00:00",
            "end_at": "2026-05-26T14:30:00",
            "duration_minutes": 30,
            "status": "scheduled",
            "notes": "knee pain",
        }
    ]


def _app(store: MailStore) -> FastAPI:
    app = FastAPI()
    app.include_router(build_frontdesk_router(store, _fake_calendar))
    return app


async def test_mail_endpoint_lists_messages(tmp_path) -> None:
    store = MailStore(root=tmp_path)
    await store.write(
        make_message(
            session_id="s1",
            kind="handoff",
            to_label="reception@prosper.health",
            subject="Callback — Jane Doe",
            body="refill",
            patient_name="Jane Doe",
            patient_phone="2025550142",
            category="prescription",
            ts=1.0,
        )
    )
    resp = TestClient(_app(store)).get("/frontdesk/mail")
    assert resp.status_code == 200
    m = resp.json()["mail"][0]
    assert m["kind"] == "handoff"
    # Full PII on the staff surface — phone must not be masked
    assert m["patient_phone"] == "2025550142"


def test_calendar_endpoint_proxies(tmp_path) -> None:
    resp = TestClient(_app(MailStore(root=tmp_path))).get(
        "/frontdesk/appointments", params={"from": "2026-05-25", "to": "2026-05-27"}
    )
    assert resp.status_code == 200
    assert resp.json()["entries"][0]["patient_name"] == "Jane Doe"


def test_root_serves_spa(tmp_path) -> None:
    resp = TestClient(_app(MailStore(root=tmp_path))).get("/frontdesk")
    assert resp.status_code == 200
    assert "Front Desk" in resp.text
