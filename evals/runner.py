"""Headless scenario runner.

For each Scenario:
1. Build a fresh SQLite EHR (file-backed in a temp path), mount via
   httpx.ASGITransport.
2. Run ``scenario.setup`` to seed the DB.
3. Snapshot patient / appointment counts.
4. Drive the dispatcher with a persona-LLM until END, an error, or
   ``max_turns``.
5. Snapshot counts again; check state expectations.
6. Run the LLM judge.
7. Return a ScenarioResult.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from evals.judge import judge_transcript
from evals.sim import PersonaSimulator
from evals.types import Scenario, ScenarioResult
from prosper.dispatcher import Dispatcher
from prosper.ehr.api import create_app
from prosper.ehr.db import get_engine, init_db
from prosper.ehr.models import Appointment, AppointmentStatus, Patient
from prosper.ehr_client import EHRClient
from prosper.flows import State
from prosper.llm import OpenAILLMAdapter


@contextmanager
def _isolated_db_env() -> Iterator[str]:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    prior = os.environ.get("PROSPER_DB_URL")
    os.environ["PROSPER_DB_URL"] = f"sqlite:///{tmp.name}"
    try:
        get_engine(reset=True)
        init_db()
        yield tmp.name
    finally:
        if prior is None:
            os.environ.pop("PROSPER_DB_URL", None)
        else:
            os.environ["PROSPER_DB_URL"] = prior
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _count_patients(session: Session) -> int:
    return session.execute(select(func.count(Patient.id))).scalar_one()


def _count_appts(session: Session, status: AppointmentStatus) -> int:
    return session.execute(
        select(func.count(Appointment.id)).where(Appointment.status == status)
    ).scalar_one()


def _evaluate_state(
    *,
    scenario: Scenario,
    transcript: list[dict],
    terminal_state: State,
    deltas: dict[str, int],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    e = scenario.expected_state
    if e.patient_count_delta != deltas["patient"]:
        reasons.append(
            f"patient_count_delta {deltas['patient']} != expected {e.patient_count_delta}"
        )
    if e.active_appointment_count_delta != deltas["active"]:
        reasons.append(
            f"active_appt_delta {deltas['active']} != expected {e.active_appointment_count_delta}"
        )
    if e.cancelled_appointment_count_delta != deltas["cancelled"]:
        reasons.append(
            f"cancelled_appt_delta {deltas['cancelled']} != expected "
            f"{e.cancelled_appointment_count_delta}"
        )
    if e.expected_terminal_state and terminal_state.value != e.expected_terminal_state:
        reasons.append(
            f"terminal state {terminal_state.value} != expected {e.expected_terminal_state}"
        )
    fired_codes = [ev["name"] for ev in transcript if ev.get("kind") in ("tool_ok", "tool_err")]
    for code in e.expected_tool_call_codes:
        if code not in fired_codes:
            reasons.append(f"expected tool {code} never fired")
    for code in e.forbidden_tool_calls:
        if code in fired_codes:
            reasons.append(f"forbidden tool {code} fired")
    return (not reasons), reasons


async def run_scenario(scenario: Scenario, *, openai_client: Any) -> ScenarioResult:
    started = time.perf_counter()
    with _isolated_db_env():
        with Session(get_engine()) as setup_session:
            scenario.setup(setup_session)
            setup_session.commit()
        with Session(get_engine()) as snap:
            before = {
                "patient": _count_patients(snap),
                "active": _count_appts(snap, AppointmentStatus.SCHEDULED),
                "cancelled": _count_appts(snap, AppointmentStatus.CANCELLED),
            }
        app = create_app()
        ehr = EHRClient.for_asgi_app(app)
        dispatcher = Dispatcher(
            llm=OpenAILLMAdapter(client=openai_client, model="gpt-4o-mini"),
            ehr_client=ehr,
        )
        async with ehr:
            sim = PersonaSimulator(client=openai_client, persona=scenario.persona)
            bot_text = await dispatcher.start()
            turns = 0
            while dispatcher.state is not State.END and turns < scenario.max_turns:
                user_text = await sim.reply_to(bot_text)
                lower = user_text.lower()
                if "thanks, bye" in lower or "goodbye" in lower or "bye!" in lower:
                    break
                bot_text = await dispatcher.handle_user_turn(user_text)
                turns += 1
        with Session(get_engine()) as snap:
            after = {
                "patient": _count_patients(snap),
                "active": _count_appts(snap, AppointmentStatus.SCHEDULED),
                "cancelled": _count_appts(snap, AppointmentStatus.CANCELLED),
            }
        deltas = {k: after[k] - before[k] for k in before}
        state_pass, reasons = _evaluate_state(
            scenario=scenario,
            transcript=dispatcher.transcript,
            terminal_state=dispatcher.state,
            deltas=deltas,
        )
        judge_pass, justification = await judge_transcript(
            client=openai_client,
            transcript=dispatcher.transcript,
            criteria=scenario.judge_criteria,
        )
        timing = dispatcher.timing.summary()
    duration = (time.perf_counter() - started) * 1000
    return ScenarioResult(
        name=scenario.name,
        state_pass=state_pass,
        state_reasons=reasons,
        judge_pass=judge_pass,
        judge_justification=justification,
        turns=turns,
        duration_ms=duration,
        transcript=dispatcher.transcript,
        timing_summary=timing,
    )
