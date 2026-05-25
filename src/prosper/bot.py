"""Pipecat pipeline wired to our dispatcher.

We don't use OpenAILLMService directly — its built-in tool-calling loop
does not know about our per-state whitelist. Instead the pipeline streams
STT text into a small adapter that calls ``Dispatcher.handle_user_turn``
and emits the dispatcher's reply via TTS. This keeps the FSM as the single
source of truth for which tools fire.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from collections.abc import AsyncIterator
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
    LLMMessagesAppendFrame,
    StartFrame,
    StartInterruptionFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import (
    RTVIConfig,
    RTVIObserver,
    RTVIProcessor,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.elevenlabs.stt import ElevenLabsRealtimeSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams

from prosper import prompts
from prosper.console.audit import AuditJSONLWriter
from prosper.console.bus import ConsoleBus
from prosper.console.server import run as run_console_server
from prosper.dispatcher import Dispatcher
from prosper.ehr_client import EHRClient
from prosper.flows import ALLOWED_TOOLS, INTERNAL_TOOLS, State
from prosper.llm import OpenAILLMAdapter
from prosper.observability.redact import redact_pii
from prosper.observers import TTSAudibleObserver

_ALLOWED_EHR_SCHEMES = {"http", "https"}


def _validated_ehr_url() -> str:
    """Resolve + sanity-check the EHR base URL from env.

    SSRF context: anyone who controls .env (compromised CI, sloppy deploy)
    could repoint the bot at e.g. http://169.254.169.254/latest/meta-data
    (cloud metadata) or an internal admin endpoint, then phish the LLM into
    triggering tool calls that exfiltrate the response.

    What THIS function enforces: scheme must be http/https and a hostname must
    be present (a malformed or file:// URL fails fast at startup). It does NOT
    block private / link-local / metadata IPs — and deliberately so: the legit
    default is loopback (127.0.0.1), a parse-time IP check cannot be trusted
    anyway (DNS rebinding, IPv6, and httpx connecting by hostname not the parsed
    IP would all bypass it), and adding one would be false security. Egress
    control to dangerous IP ranges is the deploy environment's job (network
    policy / egress firewall) — see SECURITY.md.
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
# filler before the LLM turn to mask the wait. See
# 2026-05-20-latency-advanced-research.md §1.
#
# UX polish (2026-05-20): rotate the filler by state so a single call that
# touches several tool-firing states ("One moment. ... One moment. ...
# One moment.") instead hears varied, action-appropriate acknowledgements
# ("One moment. ... Let me check. ... Looking that up."). IDENTIFY_PATIENT
# is fixed to "One moment." to preserve the dispatcher-processor unit test
# contract; the other states use action-specific phrasings that hint at
# *why* the bot paused.
# Single source of truth for per-state filler copy lives in `prompts.py`
# (keyed by state name); this dict re-keys by State enum for fast lookup in
# the hot path. Adding a state? Update prompts.STATE_FILLERS — the assertion
# below catches the drift on import.
_STATE_FILLERS: dict[State, str] = {
    State[name]: text for name, text in prompts.STATE_FILLERS.items()
}

# Latency-gated filler emission. Earlier revisions pushed a per-state
# filler unconditionally before every dispatcher turn. That made the bot
# say "One moment. Looking you up — this can take a few seconds." in front
# of a 30 ms local SQLite lookup — artificial dead air the caller doesn't
# need. The gate below predicts the next turn's latency from
# ``dispatcher.timing.summary()`` (p95 per tool, warm) plus an LLM
# baseline and only emits the filler if the prediction exceeds
# ``FILLER_LATENCY_THRESHOLD_MS``. Cold start (no history) falls back to
# ``DEFAULT_TOOL_LATENCY_MS`` so the first slow turn still gets a filler.
LLM_BASELINE_LATENCY_MS = 300
DEFAULT_TOOL_LATENCY_MS = 500
FILLER_LATENCY_THRESHOLD_MS = 700


def _should_emit_filler(dispatcher: Dispatcher, state: State) -> bool:
    """Predict whether the next turn will be slow enough to need a filler."""
    # Internal nav tools (route_intent) fire no EHR call and have no latency —
    # exclude them so a state whose only tool is internal (CHOOSE_INTENT) stays
    # silent instead of predicting a phantom 500 ms tool wait.
    tools = ALLOWED_TOOLS.get(state, set()) - INTERNAL_TOOLS
    if not tools:
        return False
    summary = dispatcher.timing.summary()
    if not isinstance(summary, dict):
        # Defensive: a stubbed timing collector in tests may return a
        # non-dict. Treat as cold-start → emit the filler.
        return True
    worst_tool_ms = max(
        float(summary.get(f"tool:{name}", {}).get("p95", DEFAULT_TOOL_LATENCY_MS)) for name in tools
    )
    predicted_ms = LLM_BASELINE_LATENCY_MS + worst_tool_ms
    return predicted_ms >= FILLER_LATENCY_THRESHOLD_MS


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
# risk shipping a half-working bot. Upgrade is listed in ARCHITECTURE.md "Future
# work".
_PIPECAT_VERSION = getattr(_pipecat, "__version__", "unknown")
if not _PIPECAT_VERSION.startswith("0."):
    logger.warning(
        "Pipecat {} detected; this codebase targets 0.0.100 — re-validate "
        "FrameProcessor + Frame APIs before shipping.",
        _PIPECAT_VERSION,
    )


def _extract_user_text_from_messages(messages: list[dict[str, Any]] | None) -> str:
    """Pull the user-role text from an LLMMessagesAppendFrame payload.

    The RTVI `send-text` handler produces a single-element list
    ``[{"role": "user", "content": "..."}]`` — but stay defensive in case
    future client versions batch multiple messages.
    """
    if not messages:
        return ""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content.strip()
    return ""


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
        # Transcript aggregation: ElevenLabs STT commits a SEPARATE final
        # TranscriptionFrame each time the caller pauses, so a choppy utterance
        # ("um… next week… please") used to fire one dispatcher turn PER
        # fragment — the bot answered each piece separately. We instead buffer
        # fragments and flush them as ONE turn after the caller has been quiet
        # for `_agg_window_s` (no new fragment). Window is env-tunable; the
        # _turn_lock serialises flushes so a late fragment can't run a second
        # handle_user_turn while the previous one is still in flight.
        self._agg_parts: list[str] = []
        self._agg_task: asyncio.Task[None] | None = None
        self._turn_lock = asyncio.Lock()
        self._agg_window_s = float(os.environ.get("PROSPER_TURN_AGG_S", "1.2"))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame) and not self._greeted:
            # Forward StartFrame downstream first — TTS / output transport
            # refuse all frames until they receive StartFrame. Previously we
            # consumed it here, leaving the pipeline stuck in "connecting".
            await self.push_frame(frame, direction)
            self._greeted = True
            opener = await self._dispatcher.start()
            if opener:
                await self.push_frame(TTSSpeakFrame(opener))
            return

        # Barge-in diagnostics: these fire ONLY if the input-transport VAD
        # actually detects the caller speaking. If you talk over the bot and
        # neither line appears in the log, the VAD never heard you (mic/echo —
        # the bot's own audio is masking your voice; try headphones), NOT a
        # dispatcher bug. If they DO appear but the bot keeps talking, the
        # problem is downstream flush. This is the line that ends the guessing.
        if isinstance(frame, UserStartedSpeakingFrame):
            logger.info("VAD: user started speaking (barge-in candidate)")
        elif isinstance(frame, StartInterruptionFrame):
            logger.info("VAD: INTERRUPTION fired — cancelling bot speech")

        if isinstance(frame, TranscriptionFrame) and frame.text:
            # Buffer the fragment; a quiet gap flushes the joined utterance as
            # one turn (see __init__). Do not forward the transcript downstream.
            self._buffer_user_text(frame.text)
            return

        # Chat textbox in the pipecat-react playground sends user input via
        # the RTVI `send-text` message, which the RTVIProcessor turns into an
        # LLMMessagesAppendFrame. We don't run an OpenAILLMService (our
        # dispatcher owns the LLM loop), so without this branch the frame
        # would just interrupt the bot without ever reaching the dispatcher
        # — the chat UI would look broken even while voice works.
        if isinstance(frame, LLMMessagesAppendFrame):
            text = _extract_user_text_from_messages(frame.messages)
            if text:
                await self._route_user_text(text, frame=None, direction=direction)
                return

        if isinstance(frame, EndFrame):
            # Drop any pending aggregation so a half-collected utterance does
            # not fire after the call ended.
            if self._agg_task is not None and not self._agg_task.done():
                self._agg_task.cancel()
            logger.info("Call ended in state {}", self._dispatcher.state.value)

        await self.push_frame(frame, direction)

    def _buffer_user_text(self, text: str) -> None:
        """Add a transcript fragment and (re)arm the quiet-gap flush timer."""
        stripped = text.strip()
        if stripped:
            self._agg_parts.append(stripped)
        if self._agg_task is not None and not self._agg_task.done():
            self._agg_task.cancel()
        self._agg_task = asyncio.create_task(self._flush_after_quiet())

    async def _flush_after_quiet(self) -> None:
        """After `_agg_window_s` with no new fragment, route the joined turn once."""
        try:
            await asyncio.sleep(self._agg_window_s)
        except asyncio.CancelledError:
            return  # a newer fragment arrived (or the call ended) — superseded
        parts = self._agg_parts
        self._agg_parts = []
        self._agg_task = None
        joined = " ".join(parts).strip()
        if not joined:
            return
        # Serialise: if a previous turn is still being handled (LLM in flight),
        # wait rather than run two handle_user_turn calls concurrently.
        async with self._turn_lock:
            await self._route_user_text(joined, frame=None, direction=FrameDirection.DOWNSTREAM)

    async def _route_user_text(
        self,
        raw_text: str,
        *,
        frame: Frame | None,
        direction: FrameDirection,
    ) -> None:
        """Run one user utterance through the dispatcher → TTS pipeline.

        ``frame`` is the original input frame when present (TranscriptionFrame
        path); for synthetic inputs like chat-textbox ``send-text`` it is
        None and we never forward the upstream frame downstream.
        """
        user_text = raw_text.strip()
        if not user_text:
            if frame is not None:
                await self.push_frame(frame, direction)
            return
        self._stt_end_ts = time.perf_counter()
        # HIPAA-adjacent: redact phone / DOB / email from log sinks. The
        # dispatcher still sees the raw text — only the log line is masked.
        logger.info("USER: {}", redact_pii(user_text))
        if _should_emit_filler(self._dispatcher, self._dispatcher.state):
            filler = _STATE_FILLERS.get(self._dispatcher.state)
            if filler is not None:
                await self.push_frame(TTSSpeakFrame(filler))
        try:
            reply = await self._dispatcher.handle_user_turn(user_text)
        # last-resort guard for live calls; mid-pipeline crash would kill the WebRTC session
        except Exception as e:
            logger.exception("dispatcher.handle_user_turn raised: {}", e)
            # Fire a best-effort bot_failed mail so staff know this caller
            # needs a human follow-up. The LLM-total-failure path fires its
            # own mail inside _llm_turn; this catch covers anything else
            # (EHR client crash, unexpected bug) that escapes the dispatcher.
            self._dispatcher._emit_system_failure_mail()
            reply = prompts.FALLBACK_LINES["dispatcher_crash"]
        if self._stt_end_ts is not None:
            ttft_ms = (time.perf_counter() - self._stt_end_ts) * 1000
            self._dispatcher.timing.record(
                phase="ttft",
                duration_ms=ttft_ms,
                state=self._dispatcher.state.value,
                session_id=self._dispatcher.session_id,
                turn_id=self._dispatcher.turn_id,
            )
            self._stt_end_ts = None
        logger.info("BOT[{}]: {}", self._dispatcher.state.value, redact_pii(reply))
        if reply:
            await self.push_frame(TTSSpeakFrame(reply))


def _build_dispatcher(
    openai_client: AsyncOpenAI | None = None,
    *,
    bus: ConsoleBus | None = None,
    mail_store: Any = None,
) -> Dispatcher:
    """Construct the dispatcher used for one call.

    A bus passed in here flows into the dispatcher's optional ``bus``
    parameter; every event-publishing hook then fires for the operator
    console. When tests or the eval runner build the dispatcher with no
    bus, every publish site is a no-op — backwards compatible by
    construction.

    ``mail_store`` is an optional ``MailStore`` instance for F6 front-desk
    handoffs and booking confirmations. When ``None`` (the default when the
    console is disabled), all mail writes are no-ops.
    """
    client: Any = openai_client or AsyncOpenAI()
    ehr_base = _validated_ehr_url()
    ehr = EHRClient.for_http(ehr_base)
    llm = OpenAILLMAdapter(
        client=client,
        model=os.environ.get("PROSPER_BOT_MODEL", "gpt-4o-mini"),
        fallback_model=os.environ.get("PROSPER_BOT_FALLBACK_MODEL"),
    )
    return Dispatcher(llm=llm, ehr_client=ehr, bus=bus, mail_store=mail_store)


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


@contextlib.asynccontextmanager
async def _telemetry_ctx(
    bus: ConsoleBus,
    audit: AuditJSONLWriter,
    *,
    serve_console: bool,
    mail_store: Any = None,
    calendar_fetch: Any = None,
) -> AsyncIterator[None]:
    """Record the call to the audit log, optionally binding the console UI.

    The durable side (``audit.attach(bus)`` → ``data/audit/<session>.jsonl``)
    runs on EVERY call so the standing console at ``:7861`` (run_all) can list
    + replay it later and tail it live. ``serve_console`` additionally binds
    the embedded uvicorn console server on ``:7861`` — kept OFF in run_all so
    the bot does not race the standing server for that port.

    ``run_console_server`` already attaches audit internally, so when
    ``serve_console`` is True we do not double-attach. When it is False we
    attach audit directly and skip uvicorn entirely.

    When ``mail_store`` and ``calendar_fetch`` are both supplied AND the
    console is served here, the ``/frontdesk`` router is included in the same
    uvicorn app. With ``serve_console`` False those surfaces are served by the
    standing process instead, reading the same durable stores.
    """
    if serve_console:
        async with run_console_server(
            bus,
            audit,
            store=mail_store,
            calendar_fetch=calendar_fetch,
        ):
            yield
        return
    async with audit.attach(bus):
        yield


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

    # Telemetry + mail wiring. The bus, audit writer, and MailStore are built
    # on EVERY live call — never gated — because:
    #   * mail must persist on every call (handoffs / booking confirmations →
    #     data/mail/mail.db → the front-desk inbox). Gating it dropped mail
    #     silently when run_all launched the bot with PROSPER_CONSOLE_ENABLED=0.
    #   * audit must record every call to data/audit/<session>.jsonl so the
    #     standing console at :7861 can list, replay, and live-tail it.
    # Only BINDING the embedded console uvicorn on :7861 is opt-out: run_all
    # sets PROSPER_CONSOLE_ENABLED=0 so the bot does not race the standing
    # console server for that port. Tests/evals build the dispatcher via
    # `_build_dispatcher` with bus/mail_store=None directly and never reach
    # this entrypoint, so they still touch no filesystem.
    from prosper.integrations.mail import MailStore

    serve_console = os.environ.get("PROSPER_CONSOLE_ENABLED", "1") == "1"
    bus = ConsoleBus()
    audit = AuditJSONLWriter()
    mail_store_inst: Any = MailStore()
    # Calendar fetcher for the embedded /frontdesk surface. Only consumed when
    # this process binds the console server (serve_console); the EHR client is
    # opened lazily inside the closure, so building it unconditionally is free.
    ehr_base_for_cal = _validated_ehr_url()
    _ehr_for_cal = EHRClient.for_http(ehr_base_for_cal)

    async def _calendar_fetch(from_date: Any, to_date: Any) -> Any:
        async with _ehr_for_cal:
            return await _ehr_for_cal.list_appointments_in_range(
                from_date=from_date, to_date=to_date
            )

    calendar_fetch_fn: Any = _calendar_fetch

    dispatcher = _build_dispatcher(bus=bus, mail_store=mail_store_inst)
    # Own the EHR httpx client via async-with so it's released even if the
    # browser drops mid-call and on_client_disconnected never fires (e.g.
    # process killed by SIGTERM, transport crash, exception during pipeline
    # startup). Previously __aexit__ ran only inside the disconnect handler
    # → guaranteed leak on abrupt termination and on every connect-time
    # failure.
    # Stack the telemetry context around the EHR client so the audit log
    # captures events from the very first turn — and so the embedded server
    # (when bound) is gracefully stopped even on exception paths. Audit always
    # attaches; the uvicorn :7861 bind happens only when serve_console.
    async with (
        dispatcher._ehr,  # bot owns this httpx client's lifecycle for the call
        _telemetry_ctx(
            bus,
            audit,
            serve_console=serve_console,
            mail_store=mail_store_inst,
            calendar_fetch=calendar_fetch_fn,
        ),
    ):
        dispatcher_processor = DispatcherProcessor(dispatcher)

        # RTVI handshake: pipecat-react clients block on the `bot-ready`
        # event sent from this processor. Without it the UI shows "Agent
        # connecting" forever even when audio is flowing.
        rtvi = RTVIProcessor(config=RTVIConfig(config=[]))

        # Interrupt-aware history: the observer sits downstream of TTS so it
        # sees every TTSTextFrame the engine emits. When VAD fires a
        # StartInterruptionFrame, the buffered text is handed to the
        # dispatcher so history[-1] gets truncated + marked. See
        # ``observers.TTSAudibleObserver`` and ``docs/research/interruption_design.md``.
        tts_observer = TTSAudibleObserver(
            on_interrupt=dispatcher.mark_last_assistant_interrupted,
        )

        pipeline = Pipeline(
            [
                transport.input(),
                rtvi,
                stt,
                dispatcher_processor,
                tts,
                tts_observer,
                transport.output(),
            ]
        )
        task = PipelineTask(
            pipeline,
            # allow_interruptions=True is REQUIRED for barge-in: without it
            # pipecat never cancels in-flight TTS when VAD detects the caller
            # speaking, so the bot talks over the caller ("no se calla"). The
            # VAD is already tuned for fast barge-in (see `bot()` VADParams) and
            # `TTSAudibleObserver` only records meaningfully once interruptions
            # actually fire — this flag is what makes that path live.
            params=PipelineParams(
                allow_interruptions=True,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            observers=[RTVIObserver(rtvi)],
        )

        @rtvi.event_handler("on_client_ready")
        async def on_client_ready(_rtvi):  # type: ignore[no-untyped-def]
            # Acknowledge the RTVI handshake so pipecat-react clients leave
            # the "Agent connecting" state and start rendering messages.
            await rtvi.set_bot_ready()

        @transport.event_handler("on_client_connected")
        async def on_client_connected(_transport, _client):  # type: ignore[no-untyped-def]
            # Bind the per-call session id into loguru so every logger.info()
            # below carries it without us having to thread it explicitly.
            logger.configure(extra={"session_id": dispatcher.session_id})
            logger.info("client connected session_id={}", dispatcher.session_id)

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(_transport, _client):  # type: ignore[no-untyped-def]
            logger.info(
                "client disconnected session_id={}; latency summary:\n{}",
                dispatcher.session_id,
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
            # VAD tuning — biased toward BARGE-IN: the caller must be able to
            # cut the bot off mid-sentence. With higher thresholds the bot
            # kept talking over the caller because it never saw a
            # user_started_speaking event to interrupt the TTS ("no se calla").
            # Tuned aggressively for BARGE-IN: live testing showed the caller
            # talking over the bot was NOT detected (0 interruptions) — the
            # bot's own audio masks a soft overlapping "yeah"/"no" so it never
            # crossed the gate. Lowered both gates so an interjection layered
            # over TTS still registers as speech and fires the interruption:
            # - confidence=0.25: lower speech-probability gate.
            # - min_volume=0.06: catch quiet interjections over the bot audio.
            # - start_secs=0.1: fire the interrupt within ~100ms.
            # - stop_secs=1.0: don't clip natural pauses between digits.
            # If this over-fires (bot interrupts itself on echo), the caller is
            # likely on speakers without echo cancellation — raise back toward
            # 0.35 / 0.15. The VAD log lines in DispatcherProcessor make the real
            # behaviour visible per call.
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    confidence=0.25,
                    start_secs=0.1,
                    stop_secs=1.0,
                    min_volume=0.06,
                ),
            ),
        ),
    }
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)
