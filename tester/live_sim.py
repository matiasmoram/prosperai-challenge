"""Drive one autonomous caller persona against the real (live-LLM) bot.

Mirrors the live branch of ``evals.runner.run_scenario`` but: (a) the caller is
a goal-seeking :class:`~tester.personas.Persona` instead of a scripted one, and
(b) it attaches a :class:`~tester.recorder.RecordingBus` so the
:mod:`tester.invariants` checks can audit the call afterwards. No per-scenario
expected DB delta — the invariants hold for *any* call.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from evals.runner import _is_persona_stop, _isolated_engine
from evals.sim import PersonaSimulator
from prosper.console.events import ConsoleEvent
from prosper.dispatcher import Dispatcher
from prosper.ehr.api import create_app
from prosper.ehr_client import EHRClient
from prosper.flows import State
from prosper.llm import OpenAILLMAdapter
from tester.personas import WORLDS, Persona
from tester.recorder import RecordingBus


@dataclass(frozen=True, slots=True)
class CallResult:
    """Everything a single simulated call produced, for reporting + auditing."""

    persona: str
    adversarial: bool
    world: str
    goal: str
    turns: int
    terminal_state: str
    outcome: str | None
    events: list[ConsoleEvent]
    transcript: list[dict[str, Any]]
    duration_ms: float
    error: str | None = None


def _terminal_outcome(events: list[ConsoleEvent]) -> str | None:
    """Return the call's terminal ``outcome`` label, or None if it never ended."""
    for e in reversed(events):
        if e.type == "outcome":
            return str(e.payload.get("outcome", "")) or None
    return None


async def simulate_call(
    persona: Persona,
    *,
    client: Any,
    model: str | None = None,
    max_turns: int = 14,
) -> CallResult:
    """Run ``persona`` against the live bot end-to-end; return a :class:`CallResult`.

    Any exception raised mid-call is captured into ``CallResult.error`` rather
    than propagated, so one bad call never aborts a batch.
    """
    bus = RecordingBus()
    started = time.perf_counter()
    error: str | None = None
    transcript: list[dict[str, Any]] = []
    state_val = "UNKNOWN"
    turns = 0
    with _isolated_engine() as engine:
        with Session(engine) as setup_session:
            WORLDS[persona.world](setup_session)
            setup_session.commit()
        app = create_app(engine=engine)
        ehr = EHRClient.for_asgi_app(app)
        bot_model = model or os.environ.get("PROSPER_BOT_MODEL", "gpt-4o-mini")
        dispatcher = Dispatcher(
            llm=OpenAILLMAdapter(client=client, model=bot_model),
            ehr_client=ehr,
            bus=bus,
        )
        async with ehr:
            sim = PersonaSimulator(client=client, persona=persona.prompt, model=model)
            try:
                bot_text = await dispatcher.start()
                while dispatcher.state is not State.END and turns < max_turns:
                    user_text = await sim.reply_to(bot_text)
                    if _is_persona_stop(user_text):
                        dispatcher.state = State.END
                        break
                    bot_text = await dispatcher.handle_user_turn(user_text)
                    turns += 1
            except Exception as exc:  # capture, don't crash the batch
                error = f"{type(exc).__name__}: {exc}"
            if dispatcher._inflight_publishes:
                await asyncio.gather(*list(dispatcher._inflight_publishes), return_exceptions=True)
        transcript = list(dispatcher.transcript)
        state_val = dispatcher.state.value
    return CallResult(
        persona=persona.name,
        adversarial=persona.adversarial,
        world=persona.world,
        goal=persona.goal,
        turns=turns,
        terminal_state=state_val,
        outcome=_terminal_outcome(bus.events),
        events=bus.events,
        transcript=transcript,
        duration_ms=(time.perf_counter() - started) * 1000,
        error=error,
    )
