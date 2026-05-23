"""Coverage-gap tests for ``prosper.bot.DispatcherProcessor`` exception path.

The try/except wrapping ``dispatcher.handle_user_turn`` was added in the
prod-readiness wave so a single LLM/EHR exception can't crash the live
Pipecat pipeline mid-call. We assert it speaks a recovery line and keeps
the pipeline alive instead of propagating the exception.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# pipecat import is expensive (~17s cold); set required env BEFORE import so
# the bot module's fail-fast guard doesn't SystemExit on this test process.
os.environ.setdefault("ELEVENLABS_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")

from pipecat.frames.frames import TranscriptionFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection

from prosper.bot import DispatcherProcessor
from prosper.flows import State


def _make_processor(dispatcher: Any) -> DispatcherProcessor:
    proc = DispatcherProcessor(dispatcher)
    # FrameProcessor.__init__ wires a lot of pipeline plumbing; for our unit
    # tests we just want to assert which frames it pushes. Stub push_frame.
    proc.push_frame = AsyncMock()  # type: ignore[method-assign]
    return proc


async def test_dispatcher_processor_speaks_recovery_line_on_exception() -> None:
    dispatcher = MagicMock()
    dispatcher.state = State.IDENTIFY_PATIENT
    dispatcher.handle_user_turn = AsyncMock(side_effect=RuntimeError("LLM 503"))
    dispatcher.timing = MagicMock()

    proc = _make_processor(dispatcher)
    frame = TranscriptionFrame(text="my phone is 2025550100", user_id="u", timestamp="t")
    await proc.process_frame(frame, FrameDirection.DOWNSTREAM)

    pushed_texts = [
        call.args[0].text
        for call in proc.push_frame.call_args_list
        if isinstance(call.args[0], TTSSpeakFrame)
    ]
    # 1) The IDENTIFY_PATIENT filler starts with "One moment." and adds a
    #    reassurance about wait time. 2) The recovery line follows — the
    #    exact wording lives in prompts.FALLBACK_LINES so we assert by
    #    constant rather than pinning a copy phrase that changes when the
    #    prompt is polished.
    from prosper.prompts import FALLBACK_LINES

    assert any(t.startswith("One moment.") for t in pushed_texts)
    assert FALLBACK_LINES["dispatcher_crash"] in pushed_texts


async def test_dispatcher_processor_records_ttft_on_normal_turn() -> None:
    """Successful turn must record a TTFT span via dispatcher.timing.record."""
    dispatcher = MagicMock()
    dispatcher.state = State.IDENTIFY_PATIENT
    dispatcher.handle_user_turn = AsyncMock(return_value="thanks!")
    dispatcher.timing = MagicMock()

    proc = _make_processor(dispatcher)
    frame = TranscriptionFrame(text="2025550100", user_id="u", timestamp="t")
    await proc.process_frame(frame, FrameDirection.DOWNSTREAM)

    # timing.record was called with phase="ttft" and a positive duration.
    record_calls = [c for c in dispatcher.timing.record.call_args_list]
    assert any(c.kwargs.get("phase") == "ttft" for c in record_calls)
    ttft_call = next(c for c in record_calls if c.kwargs.get("phase") == "ttft")
    assert ttft_call.kwargs["duration_ms"] >= 0
    assert ttft_call.kwargs["state"] == "IDENTIFY_PATIENT"


async def test_dispatcher_processor_skips_filler_in_non_tool_firing_state() -> None:
    """GREETING / CHOOSE_INTENT / END don't fire tools so no 'One moment.' filler."""
    dispatcher = MagicMock()
    dispatcher.state = State.CHOOSE_INTENT
    dispatcher.handle_user_turn = AsyncMock(return_value="book or cancel?")
    dispatcher.timing = MagicMock()

    proc = _make_processor(dispatcher)
    frame = TranscriptionFrame(text="hi", user_id="u", timestamp="t")
    await proc.process_frame(frame, FrameDirection.DOWNSTREAM)

    pushed_texts = [
        call.args[0].text
        for call in proc.push_frame.call_args_list
        if isinstance(call.args[0], TTSSpeakFrame)
    ]
    assert not any(t.startswith("One moment.") for t in pushed_texts)
    assert "book or cancel?" in pushed_texts
