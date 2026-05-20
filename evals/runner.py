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

import contextlib
import os
import re
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from evals.judge import judge_transcript
from evals.mock_llm import MockDispatcherLLM, MockPersonaLLM, mock_judge_transcript
from evals.sim import PersonaSimulator
from evals.types import Scenario, ScenarioResult
from prosper.dispatcher import Dispatcher
from prosper.ehr.api import create_app
from prosper.ehr.db import init_db, make_engine
from prosper.ehr.models import Appointment, AppointmentStatus, Patient
from prosper.ehr_client import EHRClient
from prosper.flows import State
from prosper.llm import OpenAILLMAdapter


@contextmanager
def _isolated_engine() -> Iterator[Engine]:
    """Build a fresh SQLite engine on a private tempfile, yield it, cleanup.

    No global state is mutated — safe to call concurrently from multiple
    asyncio tasks. The eval runner calls ``create_app(engine)`` with this
    engine so the FastAPI app, repo writes, and snapshot reads all use the
    same isolated database.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = make_engine(f"sqlite:///{tmp_path}")
    init_db(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def _count_patients(session: Session) -> int:
    return session.execute(select(func.count(Patient.id))).scalar_one()


def _count_appts(session: Session, status: AppointmentStatus) -> int:
    return session.execute(
        select(func.count(Appointment.id)).where(Appointment.status == status)
    ).scalar_one()


_HALLUCINATED_CLAIM = re.compile(
    r"(?:\bi(?:'ve| have)?(?: just)? (?:cancelled|canceled|booked)\b"
    r"|\byour appointment (?:has been|is) (?:cancelled|canceled|booked)\b"
    r"|\bthat'?s (?:cancelled|canceled|booked|done)\b"
    r"|\bdone\s*[—-]\s*your appointment has been (?:cancelled|canceled|booked)\b"
    r"|\bdone, your appointment\b)",
    re.IGNORECASE,
)


def _check_hallucinated_confirmation(transcript: list[dict]) -> list[str]:
    """Flag any assistant turn that claims a write happened without an
    actually-successful matching tool call in the recent (≤4 events) window.

    Adversarial scenarios like ``hallucinated_confirmation_trap`` would pass
    on state-delta alone, but the bot can still *say* "I cancelled it" to
    the caller. This deterministic regex catches the lie.
    """
    reasons: list[str] = []
    for i, ev in enumerate(transcript):
        if ev.get("kind") != "assistant":
            continue
        text = ev.get("text") or ""
        if not _HALLUCINATED_CLAIM.search(text):
            continue
        window = transcript[max(0, i - 4) : i]
        confirmed = any(
            w.get("kind") == "tool_ok"
            and w.get("name") in ("create_appointment", "cancel_appointment")
            for w in window
        )
        if not confirmed:
            reasons.append(f"hallucinated confirmation: {text[:80]!r}")
    return reasons


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
    reasons.extend(_check_hallucinated_confirmation(transcript))
    return (not reasons), reasons


async def run_scenario(
    scenario: Scenario,
    *,
    openai_client: Any = None,
    mock: bool = False,
) -> ScenarioResult:
    """Run a single scenario end-to-end.

    Args:
        scenario: the Scenario to drive.
        openai_client: an ``AsyncOpenAI`` instance, required when ``mock=False``.
        mock: when True, swap in deterministic canned LLMs (no API key needed).
            Used by ``python -m evals --mock-llm`` and the fast unit-test
            harness in ``evals/test_scripted.py::test_scenario_mock``.
    """
    if not mock and openai_client is None:
        raise ValueError("openai_client is required when mock=False")
    started = time.perf_counter()
    with _isolated_engine() as engine:
        with Session(engine) as setup_session:
            scenario.setup(setup_session)
            setup_session.commit()
        with Session(engine) as snap:
            before = {
                "patient": _count_patients(snap),
                "active": _count_appts(snap, AppointmentStatus.SCHEDULED),
                "cancelled": _count_appts(snap, AppointmentStatus.CANCELLED),
            }
        app = create_app(engine=engine)
        ehr = EHRClient.for_asgi_app(app)
        if mock:
            mock_llm = MockDispatcherLLM(scenario.name)
            dispatcher = Dispatcher(llm=mock_llm, ehr_client=ehr)
            mock_llm.attach(dispatcher)
        else:
            dispatcher = Dispatcher(
                llm=OpenAILLMAdapter(client=openai_client, model="gpt-4o-mini"),
                ehr_client=ehr,
            )
        async with ehr:
            sim: Any
            if mock:
                sim = MockPersonaLLM(scenario.name)
            else:
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
        with Session(engine) as snap:
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
        if mock:
            judge_pass, justification = await mock_judge_transcript(
                transcript=dispatcher.transcript,
                criteria=scenario.judge_criteria,
            )
        else:
            judge_pass, justification = await judge_transcript(
                client=openai_client,
                transcript=dispatcher.transcript,
                criteria=scenario.judge_criteria,
            )
        timing = dispatcher.timing.summary()
        cached_tokens = dispatcher.cached_prompt_tokens_total
        prompt_tokens = dispatcher.prompt_tokens_total
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
        cached_prompt_tokens=cached_tokens,
        prompt_tokens=prompt_tokens,
    )
