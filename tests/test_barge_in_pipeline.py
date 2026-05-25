"""Offline pipeline integration for barge-in + call-cutoff — the call bugs that
are most noticeable to a caller (bot won't stop when interrupted; a half-said
utterance fires after the line drops).

These were previously "verified by the human operator in staging" only (see the
header of ``tests/test_barge_in.py``). `test_barge_in.py` exercises the
dispatcher's `mark_last_assistant_interrupted` in isolation, and
`test_observers.py` exercises `TTSAudibleObserver` with a stub callback. Neither
wires the two together, so a regression in the *propagation* — observer flushing
the audible prefix into the REAL dispatcher, or the aggregation timer firing a
stale turn after a hang-up — would pass both suites and only surface on a live
call.

This module closes that gap WITHOUT live audio or API keys by driving the two
Pipecat processors with injected frames in pipeline order. Deterministic, $0,
runs in `make verify`. The full acoustic TTS→STT round-trip (which would also
catch STT mis-transcription) stays deferred — see `FUTURE.md` §2.4.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx

# pipecat import is expensive + bot.py fail-fasts on missing env; preload before
# importing anything that transitively pulls bot.py.
os.environ.setdefault("ELEVENLABS_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    EndFrame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from prosper.bot import DispatcherProcessor
from prosper.dispatcher import Dispatcher, LLMClientProtocol, LLMReply
from prosper.ehr_client import EHRClient
from prosper.flows import State
from prosper.observers import TTSAudibleObserver


def _tts(text: str) -> TTSTextFrame:
    """TTSTextFrame requires an aggregation tag in pipecat 0.0.100."""
    return TTSTextFrame(text=text, aggregated_by="word")


class _NoOpLLM(LLMClientProtocol):
    """LLM stub — Group A drives the interruption frames directly; no generate() needed."""

    async def generate(
        self, *, state: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMReply:
        return LLMReply(text="")


def _make_dispatcher() -> Dispatcher:
    client = EHRClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        base_url="http://ehr-test",
    )
    return Dispatcher(llm=_NoOpLLM(), ehr_client=client, session_id="test-bargein-pipeline")


# ---------------------------------------------------------------------------
# Group A: TTSAudibleObserver → REAL dispatcher propagation (the staging gap)
# ---------------------------------------------------------------------------


async def test_interrupt_truncates_real_dispatcher_history() -> None:
    """Barge-in mid-bot-turn flushes the audible prefix into the real dispatcher,
    truncating history[-1] and marking it — end to end, observer → dispatcher."""
    d = _make_dispatcher()
    full = "Booking your visit. We have Monday at 9, Tuesday at 10, or Wednesday at 2."
    d.history.append({"role": "assistant", "content": full})

    obs = TTSAudibleObserver(on_interrupt=d.mark_last_assistant_interrupted)
    obs.push_frame = AsyncMock()  # type: ignore[method-assign]

    # Bot starts speaking, TTS emits the audible prefix, caller barges in.
    await obs.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await obs.process_frame(
        _tts("Booking your visit. We have Monday at 9"), FrameDirection.DOWNSTREAM
    )
    await obs.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

    content = str(d.history[-1]["content"])
    assert content == "Booking your visit. We have Monday at 9… [INTERRUPTED by user]"
    # The unspoken tail must be gone — the LLM must not believe it said it.
    assert "Tuesday" not in content
    assert "Wednesday" not in content


async def test_spurious_interrupt_between_turns_preserves_completed_turn() -> None:
    """An InterruptionFrame with no active bot turn (bot not speaking) must NOT
    overwrite a fully-spoken assistant turn with [NOT HEARD]. Worst-case clobber."""
    d = _make_dispatcher()
    d.history.append({"role": "assistant", "content": "You're all set — goodbye!"})

    obs = TTSAudibleObserver(on_interrupt=d.mark_last_assistant_interrupted)
    obs.push_frame = AsyncMock()  # type: ignore[method-assign]

    # No BotStartedSpeakingFrame → observer is not in a speaking turn.
    await obs.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

    assert d.history[-1]["content"] == "You're all set — goodbye!"
    assert "[INTERRUPTED by user]" not in str(d.history[-1]["content"])
    assert "[NOT HEARD]" not in str(d.history[-1]["content"])


# ---------------------------------------------------------------------------
# Group B: DispatcherProcessor aggregation under interruption / call-cutoff
# ---------------------------------------------------------------------------


def _mock_dispatcher(state: State = State.BOOK_FLOW) -> MagicMock:
    d = MagicMock()
    d.state = state
    d.handle_user_turn = AsyncMock(return_value="ok")
    d.timing = MagicMock()
    return d


def _make_processor(dispatcher: Any, *, window_s: float) -> DispatcherProcessor:
    proc = DispatcherProcessor(dispatcher)
    proc.push_frame = AsyncMock()  # type: ignore[method-assign]
    proc._agg_window_s = window_s
    return proc


async def test_endframe_cancels_pending_aggregation_no_stale_turn() -> None:
    """Caller hangs up mid-utterance: the buffered fragment must NOT fire a turn
    after the call ended. EndFrame cancels the pending aggregation task."""
    d = _mock_dispatcher()
    # Large window so the flush can't fire on its own — only the EndFrame should act.
    proc = _make_processor(d, window_s=30.0)

    await proc.process_frame(
        TranscriptionFrame(text="I'd like to bo", user_id="u", timestamp="t"),
        FrameDirection.DOWNSTREAM,
    )
    assert proc._agg_task is not None and not proc._agg_task.done()

    await proc.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
    # Let the cancellation settle.
    await asyncio.gather(proc._agg_task, return_exceptions=True)

    assert proc._agg_task.cancelled()
    d.handle_user_turn.assert_not_awaited()


async def test_bargein_does_not_drop_buffered_utterance() -> None:
    """An InterruptionFrame arriving mid-utterance must NOT cancel aggregation —
    the caller's barge-in words still flush as ONE turn. (Cancelling the agg task
    on interrupt would silently DROP the interrupting utterance — the tempting
    wrong fix; this guards against it.)"""
    d = _mock_dispatcher()
    proc = _make_processor(d, window_s=0.05)

    # Caller starts speaking (fragment buffered), an interruption frame flows
    # through the pipeline mid-utterance, then the rest of the utterance arrives.
    await proc.process_frame(
        TranscriptionFrame(text="actually", user_id="u", timestamp="t"),
        FrameDirection.DOWNSTREAM,
    )
    await proc.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
    await proc.process_frame(
        TranscriptionFrame(text="move it to Monday", user_id="u", timestamp="t"),
        FrameDirection.DOWNSTREAM,
    )
    assert proc._agg_task is not None
    await proc._agg_task

    d.handle_user_turn.assert_awaited_once()
    (joined,), _ = d.handle_user_turn.await_args
    assert joined == "actually move it to Monday"
