"""Pipecat pipeline wired to our dispatcher.

We don't use OpenAILLMService directly — its built-in tool-calling loop
does not know about our per-state whitelist. Instead the pipeline streams
STT text into a small adapter that calls ``Dispatcher.handle_user_turn``
and emits the dispatcher's reply via TTS. This keeps the FSM as the single
source of truth for which tools fire.
"""
from __future__ import annotations

import os
from typing import Any, Optional

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
from prosper.llm import OpenAILLMAdapter

load_dotenv(override=True)


class DispatcherProcessor(FrameProcessor):
    """Bridges Pipecat frames to our dispatcher.

    Final TranscriptionFrame -> dispatcher.handle_user_turn -> bot reply
    queued as TTSSpeakFrame downstream.
    """

    def __init__(self, dispatcher: Dispatcher) -> None:
        super().__init__()
        self._dispatcher = dispatcher
        self._greeted = False

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
            logger.info("USER: {}", user_text)
            reply = await self._dispatcher.handle_user_turn(user_text)
            logger.info("BOT[{}]: {}", self._dispatcher.state.value, reply)
            if reply:
                await self.push_frame(TTSSpeakFrame(reply))
            return

        if isinstance(frame, EndFrame):
            logger.info("Call ended in state {}", self._dispatcher.state.value)

        await self.push_frame(frame, direction)


def _build_dispatcher(openai_client: Optional[AsyncOpenAI] = None) -> Dispatcher:
    client: Any = openai_client or AsyncOpenAI()
    ehr_base = os.environ.get("PROSPER_EHR_URL", "http://127.0.0.1:8000")
    ehr = EHRClient.for_http(ehr_base)
    llm = OpenAILLMAdapter(
        client=client,
        model=os.environ.get("PROSPER_BOT_MODEL", "gpt-4o-mini"),
    )
    return Dispatcher(llm=llm, ehr_client=ehr)


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    elevenlabs_key = os.environ["ELEVENLABS_API_KEY"]
    stt = ElevenLabsRealtimeSTTService(api_key=elevenlabs_key)
    tts = ElevenLabsTTSService(
        api_key=elevenlabs_key, voice_id="SAz9YHcvj6GT2YYXdXww"
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
