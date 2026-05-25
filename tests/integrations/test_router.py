"""/frontdesk router: mail list + calendar proxy + SPA serve."""

from __future__ import annotations

import asyncio
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


def test_calendar_endpoint_degrades_to_503_when_ehr_unreachable(tmp_path) -> None:
    """A calendar-fetch failure must surface as a 503 with a reason, not an
    opaque 500 (the EHR being down shouldn't crash the staff calendar view)."""

    async def _failing_calendar(from_date: date, to_date: date) -> list[dict[str, Any]]:
        raise RuntimeError("EHR unreachable")

    app = FastAPI()
    app.include_router(build_frontdesk_router(MailStore(root=tmp_path), _failing_calendar))
    resp = TestClient(app, raise_server_exceptions=False).get(
        "/frontdesk/appointments", params={"from": "2026-05-25", "to": "2026-05-27"}
    )
    assert resp.status_code == 503
    assert resp.json()["error"] == "calendar_unavailable"


def test_health_reports_mail_count_and_ehr_reachable(tmp_path) -> None:
    """/frontdesk/health surfaces the mail count and a True reachable flag
    when the calendar fetch succeeds — a one-glance wiring probe."""
    store = MailStore(root=tmp_path)
    resp = TestClient(_app(store)).get("/frontdesk/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mail_count"] == 0
    assert body["ehr_reachable"] is True


def test_health_reports_mail_count_after_write(tmp_path) -> None:
    """mail_count tracks the store: a written message bumps it."""
    store = MailStore(root=tmp_path)

    async def _write() -> None:
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

    asyncio.run(_write())
    resp = TestClient(_app(store)).get("/frontdesk/health")
    assert resp.json()["mail_count"] == 1


def test_health_reports_ehr_unreachable_without_raising(tmp_path) -> None:
    """When the EHR is down, health degrades to ehr_reachable=False (still 200)
    rather than letting the calendar fetch failure escape."""

    async def _failing_calendar(from_date: date, to_date: date) -> list[dict[str, Any]]:
        raise RuntimeError("EHR unreachable")

    app = FastAPI()
    app.include_router(build_frontdesk_router(MailStore(root=tmp_path), _failing_calendar))
    resp = TestClient(app, raise_server_exceptions=False).get("/frontdesk/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ehr_reachable"] is False
    assert body["mail_count"] == 0


def test_root_serves_spa(tmp_path) -> None:
    resp = TestClient(_app(MailStore(root=tmp_path))).get("/frontdesk")
    assert resp.status_code == 200
    assert "Front Desk" in resp.text


def test_static_js_is_served(tmp_path) -> None:
    """Regression: the SPA's JS must load. router.mount() on a prefixed
    APIRouter 404'd, so the page rendered as an inert shell."""
    resp = TestClient(_app(MailStore(root=tmp_path))).get("/frontdesk/static/frontdesk.js")
    assert resp.status_code == 200
    assert "pollMail" in resp.text


def test_static_rejects_traversal(tmp_path) -> None:
    """A path-traversal filename must not escape the static dir."""
    resp = TestClient(_app(MailStore(root=tmp_path))).get(
        "/frontdesk/static/..%2f..%2f..%2fetc%2fpasswd"
    )
    assert resp.status_code == 404
