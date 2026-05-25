"""Front-desk (Mail + Calendar) server — persistent staff surface.

The live bot only mounts ``/frontdesk`` per WebRTC connection, so on its own the
staff app is not viewable as a standing site. This server runs it persistently,
reading the SAME durable stores the bot writes to, so a mail sent in the middle
of a call shows up here within a couple of seconds and the calendar reflects
real bookings.

Two modes:

* **real** (default): mail from the real ``data/mail/`` JSONL store (where the
  running bot appends booking-confirmation / handoff / failure mail), calendar
  proxied from the live EHR at ``PROSPER_EHR_URL`` (default ``:8000``). Start the
  EHR (``make seed`` + ``make ehr``) and the bot (``make bot``); place a call via
  ``/call``; watch mail + appointments appear here live.
* **--demo**: a self-contained preview with an isolated seeded EHR + a few sample
  mails in a temp dir — needs no other process running.

Usage:
    uv run python scripts/frontdesk_server.py            # real, port 7902
    uv run python scripts/frontdesk_server.py --demo     # isolated preview

Run topology (real mode — three processes, one shared DB + one shared mail store):

    make seed && make ehr                       # EHR  :8000  (clinical DB)
    make bot                                    # bot  :7860  (place calls via /call)
    uv run python scripts/frontdesk_server.py   # front-desk :7902

Then call via ``/call``: a booking-confirmation / handoff mail written mid-call
lands in ``data/mail/mail.db`` and surfaces here within the SPA's ~2s poll, and
the calendar reflects the live EHR booking. ``GET /frontdesk/health`` returns
``{mail_count, ehr_reachable}`` to confirm both legs are wired.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
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

CalendarFetch = Callable[[date, date], Awaitable[list[dict[str, Any]]]]


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
            session,
            first_name="Ada",
            last_name="Lovelace",
            dob=date(1990, 12, 10),
            phone="+12025550100",
        ),
        repo.create_patient(
            session,
            first_name="John",
            last_name="Smith",
            dob=date(1985, 6, 1),
            phone="+12025550111",
        ),
        repo.create_patient(
            session,
            first_name="Grace",
            last_name="Hopper",
            dob=date(1979, 3, 22),
            phone="+12025550122",
        ),
    ]
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
    """Sample staff-inbox mail (Reception / Doctor) so the inbox is populated."""
    now = time.time()
    return [
        make_message(
            session_id="demo-1",
            kind="booking_confirmation",
            to_label="Dr. Patel",
            subject="New appointment — Ada Lovelace at tomorrow 10:00",
            body="Ada Lovelace (+1 202 555 0100) booked a visit with Dr. Patel for tomorrow 10:00.",
            patient_name="Ada Lovelace",
            patient_phone="+1 202 555 0100",
            ts=now - 90,
        ),
        make_message(
            session_id="demo-2",
            kind="handoff",
            to_label="Reception",
            subject="Callback for John Smith — insurance_question",
            body="John Smith (+1 202 555 0111) asked for a callback.\n"
            "Reason: insurance_question. Callback wanted: yes.\n\n"
            "Wants to confirm coverage before booking.",
            patient_name="John Smith",
            patient_phone="+1 202 555 0111",
            category="insurance_question",
            ts=now - 600,
        ),
        make_message(
            session_id="demo-3",
            kind="handoff",
            to_label="Reception",
            subject="Callback for Grace Hopper — identity_unmatched",
            body="Grace Hopper (+1 202 555 0122) could not be matched after two attempts.\n"
            "Reason: identity_unmatched. Callback wanted: yes.\n\nPlease verify identity.",
            patient_name="Grace Hopper",
            patient_phone="+1 202 555 0122",
            category="identity_unmatched",
            ts=now - 1800,
        ),
        make_message(
            session_id="demo-4",
            kind="bot_failed",
            to_label="Reception",
            subject="System failure — follow up with Unknown caller",
            body="The assistant's AI failed completely mid-call. The caller heard a short "
            "apology line. Please call them back.",
            patient_name="Unknown caller",
            patient_phone="+1 202 555 0199",
            category="system_failure",
            ts=now - 3600,
        ),
    ]


@contextlib.asynccontextmanager
async def _real_calendar_fetch() -> AsyncIterator[CalendarFetch]:
    """Calendar fetcher backed by the live EHR at PROSPER_EHR_URL."""
    ehr_url = os.environ.get("PROSPER_EHR_URL", "http://127.0.0.1:8000")
    ehr = EHRClient.for_http(ehr_url)
    async with ehr:

        async def fetch(from_date: date, to_date: date) -> list[dict[str, Any]]:
            return await ehr.list_appointments_in_range(from_date=from_date, to_date=to_date)

        yield fetch


@contextlib.asynccontextmanager
async def _demo_calendar_fetch() -> AsyncIterator[CalendarFetch]:
    """Calendar fetcher backed by an isolated, seeded in-process EHR."""
    with _isolated_engine() as engine:
        with Session(engine) as s:
            _seed_ehr(s)
        ehr = EHRClient.for_asgi_app(create_app(engine=engine))
        async with ehr:

            async def fetch(from_date: date, to_date: date) -> list[dict[str, Any]]:
                return await ehr.list_appointments_in_range(from_date=from_date, to_date=to_date)

            yield fetch


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--demo", action="store_true", help="isolated seeded EHR + sample mail (no other process)"
    )
    args = parser.parse_args()

    port = int(os.environ.get("PROSPER_CONSOLE_PORT", "7902"))
    bus = ConsoleBus()
    audit = AuditJSONLWriter()

    if args.demo:
        store = MailStore(root=Path(mkdtemp(prefix="prosper-frontdesk-demo-")))
        for m in _demo_messages():
            await store.write(m)
        calendar_cm = _demo_calendar_fetch()
        mode = "demo (isolated, seeded)"
    else:
        # Real durable store the running bot appends to (default data/mail/).
        store = MailStore()
        calendar_cm = _real_calendar_fetch()
        ehr_url = os.environ.get("PROSPER_EHR_URL", "http://127.0.0.1:8000")
        mode = f"real (mail={store.root}, EHR={ehr_url})"

    async with calendar_cm as calendar_fetch:
        print(f"\nFront-desk serving on http://127.0.0.1:{port}/frontdesk  [{mode}]")
        if not args.demo:
            print("  Start the EHR (`make seed && make ehr`) and the bot (`make bot`),")
            print("  then place a call via /call — mail + bookings appear here within ~2s.")
        print("  Ctrl-C to stop.\n")
        async with run_console(bus, audit, port=port, store=store, calendar_fetch=calendar_fetch):
            while True:
                await asyncio.sleep(3600)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
