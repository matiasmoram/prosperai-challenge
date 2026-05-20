"""Pipecat pipeline wired to our dispatcher.

We don't use OpenAILLMService directly — its built-in tool-calling loop
does not know about our per-state whitelist. Instead the pipeline streams
STT text into a small adapter that calls ``Dispatcher.handle_user_turn``
and emits the dispatcher's reply via TTS. This keeps the FSM as the single
source of truth for which tools fire.
"""

from __future__ import annotations

import os
import time
from typing import Any

import pipecat as _pipecat
from dotenv import load_dotenv
from loguru import logger
from openai import AsyncOpenAI
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    StartFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.elevenlabs.stt import ElevenLabsRealtimeSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams

from prosper.dispatcher import Dispatcher
from prosper.ehr_client import EHRClient
from prosper.flows import State
from prosper.llm import OpenAILLMAdapter

# States that always trigger at least one EHR HTTP call. Inject a brief
# filler "one moment" before the LLM turn to mask the wait. See
# 2026-05-20-latency-advanced-research.md §1.
_TOOL_FIRING_STATES = {
    State.IDENTIFY_PATIENT,
    State.BOOK_FLOW,
    State.CANCEL_FLOW,
    State.CONFIRM_BOOK,
    State.CONFIRM_CANCEL,
    State.REGISTER_PATIENT,
}

load_dotenv(override=True)


# NOTE on pipecat aggregation_timeout (web research finding, 2026):
# Pipecat's default LLMUserAggregator carries a 1.0s aggregation_timeout that
# is widely reported as the #1 latency complaint. We do NOT use the user
# aggregator at all — DispatcherProcessor consumes TranscriptionFrame directly
# (see Pipeline construction below) and dispatches to our FSM. So that tax
# does not apply to this pipeline. If a future change inserts an aggregator,
# pass ``LLMUserAggregatorParams(aggregation_timeout=0.3)`` to avoid the
# regression.

# NOTE on pipecat version: 0.0.100 is intentional. v1.0.0 (released
# 2026-04-14) carries breaking changes and migrating mid-submission would
# risk shipping a half-working bot. Upgrade is listed in SOLUTION.md "Future
# work".
_PIPECAT_VERSION = getattr(_pipecat, "__version__", "unknown")
if not _PIPECAT_VERSION.startswith("0."):
    logger.warning(
        "Pipecat {} detected; this codebase targets 0.0.100 — re-validate "
        "FrameProcessor + Frame APIs before shipping.",
        _PIPECAT_VERSION,
    )


class DispatcherProcessor(FrameProcessor):
    """Bridges Pipecat frames to our dispatcher.

    Final TranscriptionFrame -> dispatcher.handle_user_turn -> bot reply
    queued as TTSSpeakFrame downstream.
    """

    def __init__(self, dispatcher: Dispatcher) -> None:
        super().__init__()
        self._dispatcher = dispatcher
        self._greeted = False
        # TTFT (time-to-first-token, end-of-user-speech → start-of-bot-speech):
        # the canonical voice-agent metric in 2026. Recorded by stamping
        # _stt_end_ts on each final TranscriptionFrame and computing the
        # delta once the dispatcher hands back the bot's reply (the LLM
        # response is the first thing TTS will emit).
        self._stt_end_ts: float | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame) and not self._greeted:
            self._greeted = True
            opener = await self._dispatcher.start()
            if opener:
                await self.push_frame(TTSSpeakFrame(opener))
            return

        if isinstance(frame, TranscriptionFrame) and frame.text:
            user_text = frame.text.strip()
            if not user_text:
                await self.push_frame(frame, direction)
                return
            self._stt_end_ts = time.perf_counter()
            logger.info("USER: {}", user_text)
            # Filler speech: in tool-firing states the LLM round-trip + EHR
            # call easily exceeds 800ms. Push a brief filler so the caller
            # hears acknowledgement immediately rather than dead air.
            if self._dispatcher.state in _TOOL_FIRING_STATES:
                await self.push_frame(TTSSpeakFrame("One moment."))
            reply = await self._dispatcher.handle_user_turn(user_text)
            if self._stt_end_ts is not None:
                ttft_ms = (time.perf_counter() - self._stt_end_ts) * 1000
                self._dispatcher.timing.record(
                    phase="ttft",
                    duration_ms=ttft_ms,
                    state=self._dispatcher.state.value,
                )
                self._stt_end_ts = None
            logger.info("BOT[{}]: {}", self._dispatcher.state.value, reply)
            if reply:
                await self.push_frame(TTSSpeakFrame(reply))
            return

        if isinstance(frame, EndFrame):
            logger.info("Call ended in state {}", self._dispatcher.state.value)

        await self.push_frame(frame, direction)


def _build_dispatcher(openai_client: AsyncOpenAI | None = None) -> Dispatcher:
    client: Any = openai_client or AsyncOpenAI()
    ehr_base = os.environ.get("PROSPER_EHR_URL", "http://127.0.0.1:8000")
    ehr = EHRClient.for_http(ehr_base)
    llm = OpenAILLMAdapter(
        client=client,
        model=os.environ.get("PROSPER_BOT_MODEL", "gpt-4o-mini"),
        fallback_model=os.environ.get("PROSPER_BOT_FALLBACK_MODEL"),
    )
    return Dispatcher(llm=llm, ehr_client=ehr)


async def _startup_health_check() -> None:
    """Probe EHR /health before accepting clients. Soft-fails (logs only).

    Surfaces the most-likely failure-at-call-time (EHR not running) before a
    caller sits through the greeting only to hit a tool error. We do NOT
    block boot — the bot still starts so the operator can see the warning
    in the logs and fix it.
    """
    import httpx

    ehr_base = os.environ.get("PROSPER_EHR_URL", "http://127.0.0.1:8000")
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{ehr_base}/health")
            if r.status_code == 200:
                logger.info("EHR health-check OK ({})", ehr_base)
            else:
                logger.warning("EHR health-check returned {} from {}", r.status_code, ehr_base)
    except Exception as e:
        logger.warning(
            "EHR health-check FAILED ({} on {}); the bot will run but tool "
            "calls will return ehr_error until the EHR is reachable",
            type(e).__name__,
            ehr_base,
        )


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    await _startup_health_check()
    elevenlabs_key = os.environ["ELEVENLABS_API_KEY"]
    stt = ElevenLabsRealtimeSTTService(api_key=elevenlabs_key)
    # ElevenLabs Flash v2.5 (per 2026-05-20 latency research) — much lower
    # first-audio latency than the default; constructor-only change.
    tts = ElevenLabsTTSService(
        api_key=elevenlabs_key,
        voice_id="SAz9YHcvj6GT2YYXdXww",
        model="eleven_flash_v2_5",
    )

    dispatcher = _build_dispatcher()
    await dispatcher._ehr.__aenter__()  # noqa: SLF001 — bot owns this client for the call
    dispatcher_processor = DispatcherProcessor(dispatcher)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            dispatcher_processor,
            tts,
            transport.output(),
        ]
    )
    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):  # type: ignore[no-untyped-def]
        logger.info("client connected")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):  # type: ignore[no-untyped-def]
        logger.info("client disconnected; latency summary:\n{}", dispatcher.timing.format_table())
        await dispatcher._ehr.__aexit__(None, None, None)  # noqa: SLF001
        await task.cancel()

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
    await runner.run(task)


async def bot(runner_args: RunnerArguments) -> None:
    transport_params = {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2)),
        ),
    }
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)
