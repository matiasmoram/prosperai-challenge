"""Capture a mock-eval call's console-bus event stream for the receipt gate.

The console bus is a usable eval oracle, but nothing in the test harness
captured its events. :class:`RecordingBus` and :func:`record_call` close that
gap. ``record_call`` drives one scenario through the *offline* mock LLMs (no API
key, no network — the same machinery as ``make mock-eval``) with a recording bus
attached, and returns the ordered event stream.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.orm import Session

import prosper.llm as _prosper_llm
from evals.mock_llm import MockDispatcherLLM, MockPersonaLLM, MockTriageClient
from evals.runner import _is_persona_stop, _isolated_engine
from evals.types import Scenario
from prosper.console.bus import ConsoleBus
from prosper.console.events import ConsoleEvent
from prosper.dispatcher import Dispatcher
from prosper.ehr.api import create_app
from prosper.ehr_client import EHRClient
from prosper.flows import State


class RecordingBus(ConsoleBus):
    """A :class:`ConsoleBus` that retains every published event in order."""

    def __init__(self) -> None:
        """Initialise an empty bus plus an in-publish-order event log."""
        super().__init__()
        self.events: list[ConsoleEvent] = []

    async def publish(self, event: ConsoleEvent) -> None:
        """Record ``event`` (before any await, so order is publish order) then fan out."""
        self.events.append(event)
        await super().publish(event)


async def record_call(scenario: Scenario) -> list[ConsoleEvent]:
    """Run ``scenario`` offline through the mock LLMs; return its event stream.

    Mirrors the mock branch of ``evals.runner.run_scenario`` but attaches a
    :class:`RecordingBus` and drains the dispatcher's fire-and-forget publish
    tasks before returning, so the captured stream is complete (notably the
    terminal ``outcome`` event published as the call reaches ``State.END``).
    """
    bus = RecordingBus()
    with _isolated_engine() as engine:
        with Session(engine) as setup_session:
            scenario.setup(setup_session)
            setup_session.commit()
        app = create_app(engine=engine)
        ehr = EHRClient.for_asgi_app(app)
        mock_llm = MockDispatcherLLM(scenario.name)
        dispatcher = Dispatcher(llm=mock_llm, ehr_client=ehr, bus=bus)
        mock_llm.attach(dispatcher)
        # Offline triage stub — the same install the runner does for mock mode,
        # so `suggest_specialty` never reaches the (absent) mini-LLM.
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
        # Publishes are fire-and-forget tasks; await the in-flight set so no
        # trailing event is dropped before we read `bus.events`.
        if dispatcher._inflight_publishes:
            await asyncio.gather(*list(dispatcher._inflight_publishes))
    return bus.events
