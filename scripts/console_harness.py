"""Operator-console verification harness — NOT shipped, dev tool only.

Drives the real dispatcher through deterministic mock-eval scenarios with a
``ConsoleBus`` + ``AuditJSONLWriter`` attached, persisting a full event
stream per session to ``data/audit/<session_id>.jsonl``. Then serves the
operator console so the recorded sessions can be replayed in a browser
(or by Playwright) exactly as an operator/receptionist would see them.

No API keys needed — uses the same MockDispatcherLLM / MockPersonaLLM /
MockTriageClient as ``make mock-eval``.

Usage:
    uv run python scripts/console_harness.py            # generate + serve
    uv run python scripts/console_harness.py --serve-only
    PROSPER_CONSOLE_PORT=7899 uv run python scripts/console_harness.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Repo root on sys.path so `evals` (a top-level package run normally via
# `python -m evals`) imports when this script is launched from scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session

import prosper.llm as _prosper_llm
from evals.mock_llm import MockDispatcherLLM, MockPersonaLLM, MockTriageClient
from evals.runner import _is_persona_stop, _isolated_engine
from evals.scenarios import SCENARIOS
from prosper.console.audit import AuditJSONLWriter
from prosper.console.bus import ConsoleBus
from prosper.console.events import make_event
from prosper.console.server import run as run_console
from prosper.dispatcher import Dispatcher
from prosper.ehr.api import create_app
from prosper.ehr_client import EHRClient
from prosper.flows import State

_SCEN = {s.name: s for s in SCENARIOS}

# (scenario, session_id) pairs that exercise the full console surface.
# Session ids are human-readable so the replay URL is obvious.
_PLAN: list[tuple[str, str]] = [
    ("new_patient_books", "demo-new-patient-books"),
    ("symptom_routes_to_gp", "demo-triage-gp"),
    ("book_60_minute_visit", "demo-triage-60min"),
    ("existing_patient_cancels", "demo-cancel"),
    ("reschedule_existing_appointment", "demo-reschedule"),
]


async def _generate(name: str, session_id: str, bus: ConsoleBus) -> None:
    """Run one scenario through the mock dispatcher, publishing to the bus."""
    scenario = _SCEN[name]
    with _isolated_engine() as engine:
        with Session(engine) as setup_session:
            scenario.setup(setup_session)
            setup_session.commit()
        app = create_app(engine=engine)
        ehr = EHRClient.for_asgi_app(app)
        mock_llm = MockDispatcherLLM(scenario.name)
        dispatcher = Dispatcher(llm=mock_llm, ehr_client=ehr, session_id=session_id, bus=bus)
        mock_llm.attach(dispatcher)
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
        # Drain fire-and-forget publish tasks so every event reaches audit.
        await asyncio.sleep(0.2)
    print(f"  generated {session_id}  (scenario={name}, state={dispatcher.state.value})")


async def _inject_interruption_demo(session_id: str, bus: ConsoleBus) -> None:
    """Hand-craft a short session that includes a turn_interrupted event.

    Eval scenarios have no audio path, so the only way to exercise the
    console's interruption rendering is to publish the event sequence
    directly. Mirrors what a real barge-in produces.
    """
    greeting = "Hi, you've reached Prosper Health — what's your name, and how can I help you today?"
    cut_line = "Booking 2 PM with Dr. Smith on Tuesday — confirming now"
    seq = [
        ("state_change", {"from_state": "(init)", "to_state": "GREETING", "trigger": "start"}),
        ("transcript_turn", {"role": "bot", "text": greeting, "turn_id": 0}),
        (
            "transcript_turn",
            {"role": "user", "text": "I'm Ada Lovelace, here to book", "turn_id": 1},
        ),
        (
            "state_change",
            {"from_state": "GREETING", "to_state": "IDENTIFY_PATIENT", "trigger": "go_identify"},
        ),
        ("transcript_turn", {"role": "bot", "text": cut_line, "turn_id": 2}),
        (
            "turn_interrupted",
            {"turn_id": 2, "state": "CONFIRM_BOOK", "spoken_text": "Booking 2 PM with Dr. Smi"},
        ),
        ("transcript_turn", {"role": "user", "text": "wait, no — not Tuesday", "turn_id": 3}),
        ("transcript_turn", {"role": "bot", "text": "No problem — what day works?", "turn_id": 4}),
    ]
    for etype, payload in seq:
        await bus.publish(make_event(etype, session_id, payload))
        await asyncio.sleep(0.05)
    print(f"  generated {session_id}  (hand-crafted interruption demo)")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-only", action="store_true", help="skip generation, just serve")
    args = parser.parse_args()

    bus = ConsoleBus()
    audit = AuditJSONLWriter()
    port = int(os.environ.get("PROSPER_CONSOLE_PORT", "7899"))

    if not args.serve_only:
        print("Generating console sessions…")
        async with audit.attach(bus):
            for name, sid in _PLAN:
                await _generate(name, sid, bus)
            await _inject_interruption_demo("demo-interruption", bus)
            await asyncio.sleep(0.3)  # flush last writes

    print(f"\nSessions on disk: {audit.list_sessions()}")
    print(f"\nConsole serving on http://127.0.0.1:{port}/console")
    print("Open a session directly, e.g.:")
    for _, sid in _PLAN:
        print(f"  http://127.0.0.1:{port}/console/{sid}")
    print(f"  http://127.0.0.1:{port}/console/demo-interruption")
    print(f"  http://127.0.0.1:{port}/call   (patient phone UI)")
    print("\nCtrl-C to stop.\n")

    async with run_console(bus, audit, port=port):
        # Serve until cancelled.
        while True:
            await asyncio.sleep(3600)


if __name__ == "__main__":
    import contextlib

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
