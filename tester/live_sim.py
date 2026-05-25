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
from tester.clarification import Turn, check_recovers_gracefully
from tester.noise import garble
from tester.personas import WORLDS, Persona, has_unfilled_placeholders
from tester.recorder import RecordingBus

# Write tools whose successful firing while a garble is pending = plowed-ahead.
_WRITE_TOOLS = frozenset({"create_appointment", "cancel_appointment", "reschedule_appointment"})


def _write_tool_in(events: list[ConsoleEvent]) -> str | None:
    """Name of the first successful write tool in ``events``, else None.

    Scans ``tool_call_end`` events (payload ``{tool, outcome, ...}``) for a write
    that completed ``ok`` — used to annotate which bot turn committed a change.
    """
    for e in events:
        if e.type == "tool_call_end":
            tool = str(e.payload.get("tool", ""))
            if tool in _WRITE_TOOLS and e.payload.get("outcome") == "ok":
                return tool
    return None


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
    corrupt: bool = False  # True when the caller LLM emitted unfilled [Placeholder] text
    # plowed_ahead_on_garble breaches — non-empty only for noise_profile personas.
    clarification_violations: tuple[str, ...] = ()


def _caller_utterance_corrupt(transcript: list[dict[str, Any]]) -> bool:
    """Return True if any caller turn contains an unfilled ``[Placeholder]`` bracket.

    When the caller LLM emits literal ``[PHONE]`` / ``[Your Name]`` etc. instead
    of the concrete value from its persona prompt, the run is testing garbage
    input (the bot may create a patient with phone ``[PHONE]``).  Such runs must
    be excluded from clean/violation tallies — they are not violations, just
    unusable data that would erode confidence in the continuous-gen signal.
    """
    return any(
        has_unfilled_placeholders(ev.get("text", ""))
        for ev in transcript
        if ev.get("kind") == "user"
    )


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
    clarification_violations: tuple[str, ...] = ()
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
            turn_log: list[Turn] = []
            try:
                bot_text = await dispatcher.start()
                turn_log.append(Turn(role="bot", text=bot_text))
                while dispatcher.state is not State.END and turns < max_turns:
                    clean = await sim.reply_to(bot_text)
                    # Stop on the persona's CLEAN intent — never garble the
                    # goodbye, or the bot would mishear it and never end.
                    if _is_persona_stop(clean):
                        dispatcher.state = State.END
                        break
                    heard = (
                        garble(clean, seed=turns, profile=persona.noise_profile)
                        if persona.noise_profile
                        else clean
                    )
                    # Mark garbled ONLY when noise actually changed the text.
                    # A confirmation like "yes, book that" has nothing for the
                    # number/intent profiles to corrupt → garble() is a no-op →
                    # the turn is effectively clean, and the booking that follows
                    # a clean confirmation must not be flagged plowed_ahead.
                    was_garbled = heard != clean
                    turn_log.append(
                        Turn(
                            role="caller",
                            text=heard,
                            garbled=was_garbled,
                            original=clean if was_garbled else None,
                        )
                    )
                    before = len(bus.events)
                    bot_text = await dispatcher.handle_user_turn(heard)
                    turn_log.append(
                        Turn(role="bot", text=bot_text, wrote=_write_tool_in(bus.events[before:]))
                    )
                    turns += 1
            except Exception as exc:  # capture, don't crash the batch
                error = f"{type(exc).__name__}: {exc}"
            if dispatcher._inflight_publishes:
                await asyncio.gather(*list(dispatcher._inflight_publishes), return_exceptions=True)
        transcript = list(dispatcher.transcript)
        state_val = dispatcher.state.value
        clarification_violations = tuple(v.detail for v in check_recovers_gracefully(turn_log))
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
        corrupt=_caller_utterance_corrupt(transcript),
        clarification_violations=clarification_violations,
    )
