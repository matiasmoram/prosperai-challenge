"""Pipecat pipeline wired to our dispatcher.

We don't use OpenAILLMService directly — its built-in tool-calling loop
does not know about our per-state whitelist. Instead the pipeline streams
STT text into a small adapter that calls ``Dispatcher.handle_user_turn``
and emits the dispatcher's reply via TTS. This keeps the FSM as the single
source of truth for which tools fire.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any
from urllib.parse import urlparse

# Perf wave 2 #5: load .env + fail-fast on missing credentials BEFORE the
# 17s pipecat import cascade. A misconfigured boot used to print "Bot
# ready!" 17s in then crash on first audio frame; now we print a single
# line and exit immediately.
from dotenv import load_dotenv

# override=False (best practice 2026): externally-set env wins over .env.
# Container / CI / systemd unit env vars should be authoritative. .env is a
# dev convenience only.
load_dotenv(override=False)

_REQUIRED_ENV = ("ELEVENLABS_API_KEY", "OPENAI_API_KEY")
_missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
if _missing:
    sys.stderr.write(
        f"ERROR: missing required env vars: {_missing}. "
        f"Did you 'cp env.example .env' and fill in your keys?\n"
    )
    # Exit before incurring the pipecat / silero / onnxruntime import wall
    # (~17s on this dev box). Re-raise inside test imports would be hostile,
    # so guard on a CLI-style env hint.
    if os.environ.get("PROSPER_BOT_ENTRYPOINT") == "1":
        raise SystemExit(2)

import pipecat as _pipecat
from loguru import logger
from openai import AsyncOpenAI

# Perf wave 2 #2: lazy-import Silero VAD inside `async def bot()` — alone
# it pulls onnxruntime + scipy.signal (~4.1s of the 17s cold-import wall).
# See the imports inside `async def bot` below.
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
from prosper.observability.redact import redact_pii

_ALLOWED_EHR_SCHEMES = {"http", "https"}


def _validated_ehr_url() -> str:
    """Resolve + sanity-check the EHR base URL from env.

    SSRF guard: anyone who controls .env (compromised CI, sloppy deploy)
    could repoint the bot at e.g. http://169.254.169.254/latest/meta-data
    (cloud metadata) or an internal admin endpoint, then phish the LLM into
    triggering tool calls that exfiltrate the response. We don't let that
    happen — only http/https are accepted, the URL must parse, and the
    hostname must be present. Hostname allowlisting beyond this is left to
    the deploy environment (network policy / egress firewall).
    """
    raw = os.environ.get("PROSPER_EHR_URL", "http://127.0.0.1:8000")
    parsed = urlparse(raw)
    if parsed.scheme not in _ALLOWED_EHR_SCHEMES:
        raise SystemExit(
            f"PROSPER_EHR_URL scheme must be one of {_ALLOWED_EHR_SCHEMES}, got {parsed.scheme!r}"
        )
    if not parsed.hostname:
        raise SystemExit(f"PROSPER_EHR_URL is missing a hostname: {raw!r}")
    return raw


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
            # HIPAA-adjacent: redact phone / DOB / email from log sinks. The
            # dispatcher still sees the raw text — only the log line is masked.
            logger.info("USER: {}", redact_pii(user_text))
            # Filler speech: in tool-firing states the LLM round-trip + EHR
            # call easily exceeds 800ms. Push a brief filler so the caller
            # hears acknowledgement immediately rather than dead air.
            if self._dispatcher.state in _TOOL_FIRING_STATES:
                await self.push_frame(TTSSpeakFrame("One moment."))
            # Wrap dispatcher in try/except: a stray exception (LLM 5xx after
            # all retries exhausted, EHR timeout, JSON parse error, etc.) must
            # NOT crash the Pipecat pipeline mid-call. Speak a recovery line
            # and let the caller try again. We keep _stt_end_ts reset so we
            # don't poison the next TTFT sample.
            try:
                reply = await self._dispatcher.handle_user_turn(user_text)
            # last-resort guard for live calls; mid-pipeline crash would kill the WebRTC session
            except Exception as e:
                logger.exception("dispatcher.handle_user_turn raised: {}", e)
                reply = "Sorry, I missed that — could you say it again?"
            if self._stt_end_ts is not None:
                ttft_ms = (time.perf_counter() - self._stt_end_ts) * 1000
                self._dispatcher.timing.record(
                    phase="ttft",
                    duration_ms=ttft_ms,
                    state=self._dispatcher.state.value,
                )
                self._stt_end_ts = None
            logger.info("BOT[{}]: {}", self._dispatcher.state.value, redact_pii(reply))
            if reply:
                await self.push_frame(TTSSpeakFrame(reply))
            return

        if isinstance(frame, EndFrame):
            logger.info("Call ended in state {}", self._dispatcher.state.value)

        await self.push_frame(frame, direction)


def _build_dispatcher(openai_client: AsyncOpenAI | None = None) -> Dispatcher:
    client: Any = openai_client or AsyncOpenAI()
    ehr_base = _validated_ehr_url()
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

    ehr_base = _validated_ehr_url()
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
    # Own the EHR httpx client via async-with so it's released even if the
    # browser drops mid-call and on_client_disconnected never fires (e.g.
    # process killed by SIGTERM, transport crash, exception during pipeline
    # startup). Previously __aexit__ ran only inside the disconnect handler
    # → guaranteed leak on abrupt termination and on every connect-time
    # failure.
    async with dispatcher._ehr:  # bot owns this httpx client's lifecycle for the call
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
        async def on_client_connected(_transport, _client):  # type: ignore[no-untyped-def]
            logger.info("client connected")

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(_transport, _client):  # type: ignore[no-untyped-def]
            logger.info(
                "client disconnected; latency summary:\n{}",
                dispatcher.timing.format_table(),
            )
            # Cancel the pipeline so runner.run() returns and we exit the
            # async-with cleanly. The EHR client is closed by the surrounding
            # context manager — NOT here — so SIGTERM mid-call still cleans up.
            await task.cancel()

        runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
        try:
            await runner.run(task)
        finally:
            # Belt + suspenders: if runner.run raised before the disconnect
            # handler had a chance to cancel the task, cancel it now so any
            # in-flight EHR/LLM calls drain before httpx closes the client.
            if not task.has_finished():
                await task.cancel()


async def bot(runner_args: RunnerArguments) -> None:
    # Perf wave 2 #2: Silero VAD pulls onnxruntime + scipy.signal via
    # pyloudnorm (~4.1s of the cold-import wall). Importing here means
    # boot is ~4s faster when the bot is just being inspected (tests,
    # IDE indexing, type-check tools).
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams

    transport_params = {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2)),
        ),
    }
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)
