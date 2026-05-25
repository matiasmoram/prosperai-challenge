"""Front-desk (Mail + Calendar) demo server — dev tool, NOT shipped.

Serves the staff ``/frontdesk`` single-page app on localhost so you can SEE the
mail inbox + clinic calendar without placing a real call. The live bot only
mounts ``/frontdesk`` per WebRTC connection; this harness wires the same router
(``store`` + ``calendar_fetch``) against a seeded in-process EHR + a populated
MailStore so the page has real data in both panes.

No API keys needed — pure in-process SQLite + a temp mail dir.

Usage:
    uv run python scripts/frontdesk_demo.py
    PROSPER_CONSOLE_PORT=7901 uv run python scripts/frontdesk_demo.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session

from evals.runner import _isolated_engine
from prosper.console.audit import AuditJSONLWriter
from prosper.console.bus import ConsoleBus
from prosper.console.server import run as run_console
from prosper.ehr import repository as repo
from prosper.ehr.api import create_app
from prosper.ehr.models import Provider, Slot
from prosper.ehr_client import EHRClient
from prosper.integrations.mail import MailMessage, MailStore, make_message


def _naive_utc(days_ahead: int, hour: int) -> datetime:
    """A naive-UTC datetime N days from now at the given hour (slot convention)."""
    base = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return base.replace(tzinfo=None)


def _seed_ehr(session: Session) -> None:
    """Seed providers, patients, slots, and booked appointments across the week."""
    providers = [
        Provider(name="Dr. Patel", timezone="UTC", specialty="Therapist"),
        Provider(name="Dr. Chen", timezone="UTC", specialty="Psychiatrist"),
        Provider(name="Dr. Okafor", timezone="UTC", specialty="General Practice"),
    ]
    session.add_all(providers)
    session.commit()

    patients = [
        repo.create_patient(
            session, first_name="Ada", last_name="Lovelace", dob=date(1990, 12, 10),
            phone="+12025550100",
        ),
        repo.create_patient(
            session, first_name="John", last_name="Smith", dob=date(1985, 6, 1),
            phone="+12025550111",
        ),
        repo.create_patient(
            session, first_name="Grace", last_name="Hopper", dob=date(1979, 3, 22),
            phone="+12025550122",
        ),
    ]

    # One booked appointment per (day, hour, patient, provider) spread over the
    # next 4 days so the calendar's [today, today+7] query returns a real week.
    bookings = [
        (1, 10, patients[0], providers[0]),
        (1, 14, patients[1], providers[1]),
        (2, 9, patients[2], providers[2]),
        (3, 11, patients[0], providers[1]),
        (4, 15, patients[1], providers[0]),
    ]
    for day, hour, patient, prov in bookings:
        start = _naive_utc(day, hour)
        slot = Slot(provider_id=prov.id, start_at=start, end_at=start + timedelta(minutes=30))
        session.add(slot)
        session.commit()
        repo.create_appointment(session, patient_id=patient.id, slot_id=slot.id)


def _demo_messages() -> list[MailMessage]:
    """Sample outbound mail so the inbox pane is populated, newest first."""
    now = time.time()
    return [
        make_message(
            session_id="demo-1",
            kind="booking_confirmation",
            to_label="ada.lovelace@example.com",
            subject="Your appointment is confirmed — Tue 10:00 with Dr. Patel",
            body="Hi Ada, this confirms your 30-minute therapy visit tomorrow at 10:00 "
            "with Dr. Patel. Reply or call us to change it.",
            patient_name="Ada Lovelace",
            patient_phone="+1 202 555 0100",
            ts=now - 120,
        ),
        make_message(
            session_id="demo-2",
            kind="handoff",
            to_label="reception@prosper.health",
            subject="Callback requested — John Smith",
            body="Caller asked to speak with a person about insurance coverage before "
            "booking. Wants a callback this afternoon.",
            patient_name="John Smith",
            patient_phone="+1 202 555 0111",
            category="insurance_question",
            ts=now - 600,
        ),
        make_message(
            session_id="demo-3",
            kind="handoff",
            to_label="reception@prosper.health",
            subject="Front-desk follow-up — Grace Hopper",
            body="The bot could not match the caller to a record after two attempts. "
            "Please verify identity and assist with booking.",
            patient_name="Grace Hopper",
            patient_phone="+1 202 555 0122",
            category="identity_unmatched",
            ts=now - 1800,
        ),
        make_message(
            session_id="demo-4",
            kind="bot_failed",
            to_label="reception@prosper.health",
            subject="Automated line interrupted — manual follow-up needed",
            body="The assistant hit a system error mid-call and could not complete the "
            "request. Caller may ring back; please follow up.",
            patient_name="Unknown caller",
            patient_phone="+1 202 555 0199",
            category="system_failure",
            ts=now - 3600,
        ),
    ]


async def main() -> None:
    port = int(os.environ.get("PROSPER_CONSOLE_PORT", "7901"))
    mail_root = Path(mkdtemp(prefix="prosper-frontdesk-demo-"))
    store = MailStore(root=mail_root)
    bus = ConsoleBus()
    audit = AuditJSONLWriter()

    # A fresh isolated SQLite engine (tables created by _isolated_engine),
    # seeded then served read-only for the calendar proxy.
    with _isolated_engine() as engine:
        with Session(engine) as s:
            _seed_ehr(s)
        for m in _demo_messages():
            await store.write(m)

        app = create_app(engine=engine)
        ehr = EHRClient.for_asgi_app(app)
        async with ehr:

            async def calendar_fetch(from_date: date, to_date: date) -> list[dict[str, Any]]:
                return await ehr.list_appointments_in_range(from_date=from_date, to_date=to_date)

            print(f"\nFront-desk demo serving on http://127.0.0.1:{port}/frontdesk")
            print(f"  mail root: {mail_root}")
            print("  (Ctrl-C to stop)\n")
            async with run_console(
                bus, audit, port=port, store=store, calendar_fetch=calendar_fetch
            ):
                while True:
                    await asyncio.sleep(3600)


if __name__ == "__main__":
    import contextlib

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
