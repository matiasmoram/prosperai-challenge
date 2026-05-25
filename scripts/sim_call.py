"""Simulate calls end-to-end and show that the calendar (EHR) and mail update.

Drives the REAL dispatcher + REAL in-process EHR + a REAL MailStore through
scripted scenarios (the deterministic mock LLM — no API key), then prints the
appointment count delta (what the front-desk calendar would show) and the mail
written (what the front-desk inbox would show). This exercises the same wiring a
live call uses: `Dispatcher(mail_store=…, ehr_client=…)`, `_emit_booking_confirmation`,
the EHR write, cancel, and the atomic reschedule.

Run: ``uv run python scripts/sim_call.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import prosper.llm as _prosper_llm
from evals.mock_llm import MockDispatcherLLM, MockPersonaLLM, MockTriageClient
from evals.runner import _is_persona_stop, _isolated_engine
from evals.scenarios import SCENARIOS
from prosper.dispatcher import Dispatcher
from prosper.ehr.api import create_app
from prosper.ehr.models import Appointment, AppointmentStatus
from prosper.ehr_client import EHRClient
from prosper.flows import State
from prosper.integrations.mail import MailStore

_SCEN = {s.name: s for s in SCENARIOS}


def _counts(engine) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    """(scheduled, cancelled) appointment counts in the EHR."""
    with Session(engine) as s:
        sched = s.execute(
            select(func.count(Appointment.id)).where(
                Appointment.status == AppointmentStatus.SCHEDULED
            )
        ).scalar_one()
        canc = s.execute(
            select(func.count(Appointment.id)).where(
                Appointment.status == AppointmentStatus.CANCELLED
            )
        ).scalar_one()
    return sched, canc


async def run_scenario(name: str) -> None:
    """Run one scripted call and report calendar + mail effects."""
    scenario = _SCEN[name]
    with _isolated_engine() as engine:
        with Session(engine) as setup_session:
            scenario.setup(setup_session)
            setup_session.commit()
        before = _counts(engine)

        app = create_app(engine=engine)
        ehr = EHRClient.for_asgi_app(app)
        mail = MailStore(root=Path(mkdtemp(prefix="simcall-")))
        llm = MockDispatcherLLM(scenario.name)
        dispatcher = Dispatcher(llm=llm, ehr_client=ehr, mail_store=mail)
        llm.attach(dispatcher)
        _prosper_llm._TRIAGE_CLIENT_OVERRIDE = MockTriageClient()

        async with ehr:
            sim = MockPersonaLLM(scenario.name)
            bot_text = await dispatcher.start()
            turns = 0
            while dispatcher.state is not State.END and turns < scenario.max_turns:
                user_text = await sim.reply_to(bot_text)
                if _is_persona_stop(user_text):
                    dispatcher.state = State.END
                    break
                bot_text = await dispatcher.handle_user_turn(user_text)
                turns += 1
            # Drain fire-and-forget mail writes so they land before we read.
            if dispatcher._inflight_publishes:
                await asyncio.gather(*list(dispatcher._inflight_publishes), return_exceptions=True)
            await asyncio.sleep(0.1)

        after = _counts(engine)
        mails = mail.list_messages()

    d_sched = after[0] - before[0]
    d_canc = after[1] - before[1]
    print(f"\n=== {name} ===")
    print(f"  terminal state : {dispatcher.state.value}")
    print(
        f"  CALENDAR Δ     : scheduled {before[0]}→{after[0]} ({d_sched:+d}), "
        f"cancelled {before[1]}→{after[1]} ({d_canc:+d})"
    )
    if mails:
        for m in mails:
            print(f"  MAIL written   : [{m.kind}] to {m.to_label} — {m.subject}")
    else:
        print("  MAIL written   : (none)")


async def main() -> None:
    # A booking (calendar +1 scheduled, confirmation mail to the doctor),
    # a cancellation (calendar -1 scheduled / +1 cancelled),
    # and an atomic reschedule (scheduled unchanged; appointment moved).
    for name in (
        "new_patient_books",
        "existing_patient_cancels",
        "reschedule_existing_appointment",
    ):
        await run_scenario(name)
    print("\nDone. Calendar (EHR) + mail both update from a scripted call.")


if __name__ == "__main__":
    asyncio.run(main())
