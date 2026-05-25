"""Stress suite for "caller talks while the bot is talking" — many barge-in
patterns, driven deterministically through the real interruption path
(``TTSAudibleObserver`` → real ``Dispatcher.mark_last_assistant_interrupted``,
and ``DispatcherProcessor`` aggregation). No live audio, no API keys.

This is the frame-level home of the barge-in LOGIC: an interruption arrives as a
downstream ``InterruptionFrame`` while the bot is mid-utterance; the observer
flushes the audible prefix into the dispatcher, which truncates the assistant
turn so the next LLM call sees an honest record of what the caller actually
heard. The full *acoustic* overlap (two audio streams mixed in real time through
Silero-VAD) is a live/staging concern; everything that can be made deterministic
is pinned here so a regression in overlap handling can't reach a live call
unnoticed. Complements `tests/test_barge_in_pipeline.py` (the 4 canonical cases)
and `tests/test_barge_in.py` (the dispatcher method in isolation).
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

os.environ.setdefault("ELEVENLABS_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
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

_DOWN = FrameDirection.DOWNSTREAM
_MARK = " [INTERRUPTED by user]"


def _tts(text: str) -> TTSTextFrame:
    return TTSTextFrame(text=text, aggregated_by="word")


class _NoOpLLM(LLMClientProtocol):
    async def generate(
        self, *, state: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMReply:
        return LLMReply(text="")


def _make_dispatcher() -> Dispatcher:
    client = EHRClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        base_url="http://ehr-test",
    )
    return Dispatcher(llm=_NoOpLLM(), ehr_client=client, session_id="bargein-stress")


def _observer(d: Dispatcher) -> TTSAudibleObserver:
    obs = TTSAudibleObserver(on_interrupt=d.mark_last_assistant_interrupted)
    obs.push_frame = AsyncMock()
    return obs


def _expected(spoken: str) -> str:
    """What history[-1] must read after an interrupt with this audible prefix."""
    s = spoken.strip()
    return f"{s}…{_MARK}" if s else f"[NOT HEARD]{_MARK}"


# Replies split into TTS chunks (leading spaces so concatenation rebuilds the line).
_R1 = ["Booking", " your", " visit", " with", " Dr.", " Patel", " on", " Monday."]
_R2 = ["I", " have", " three", " times:", " nine", " AM,", " ten", " AM,", " or", " two", " PM."]
_R3 = ["You're", " all", " set", " for", " Wednesday."]
_R4 = ["Let", " me", " check", " availability", " for", " you."]

# (reply, audible-chunk-count) — interrupt after 0, 1, middle, len-1, len chunks.
_CASES: list[tuple[list[str], int]] = [
    (r, k) for r in (_R1, _R2, _R3, _R4) for k in (0, 1, len(r) // 2, len(r) - 1, len(r))
]


@pytest.mark.parametrize("chunks,spoken_k", _CASES, ids=lambda v: str(v))
async def test_interrupt_at_many_points_truncates_to_audible_prefix(
    chunks: list[str], spoken_k: int
) -> None:
    """Caller barges in after the bot has said `spoken_k` chunks → the assistant
    turn is truncated to exactly that audible prefix, the rest is dropped."""
    d = _make_dispatcher()
    full = "".join(chunks)
    d.history.append({"role": "assistant", "content": full})
    obs = _observer(d)

    await obs.process_frame(BotStartedSpeakingFrame(), _DOWN)
    for c in chunks[:spoken_k]:
        await obs.process_frame(_tts(c), _DOWN)
    await obs.process_frame(InterruptionFrame(), _DOWN)

    # Exact equality is the complete guard: content is the audible prefix +
    # marker and nothing else, so any un-spoken tail chunk is provably gone.
    spoken = "".join(chunks[:spoken_k])
    assert d.history[-1]["content"] == _expected(spoken)


async def test_repeated_bargein_across_many_turns_stays_coherent() -> None:
    """Ten consecutive bot turns, each cut off at a different point. Every
    assistant turn ends marked exactly once; no cross-turn corruption."""
    d = _make_dispatcher()
    obs = _observer(d)
    n = 10
    for i in range(n):
        reply = f"This is bot turn number {i} with several trailing words here."
        words = reply.split(" ")
        d.history.append({"role": "assistant", "content": reply})
        await obs.process_frame(BotStartedSpeakingFrame(), _DOWN)
        # Speak a varying prefix, then get interrupted.
        spoken_words = words[: (i % len(words))]
        if spoken_words:
            await obs.process_frame(_tts(" ".join(spoken_words)), _DOWN)
        await obs.process_frame(InterruptionFrame(), _DOWN)
        # The caller's barge-in reply becomes the next user turn.
        d.history.append({"role": "user", "content": f"reply {i}"})

    assistant_turns = [h for h in d.history if h.get("role") == "assistant"]
    assert len(assistant_turns) == n
    for h in assistant_turns:
        assert str(h["content"]).count(_MARK) == 1, h["content"]


async def test_rapid_multiple_interrupts_in_one_turn_mark_once() -> None:
    """Three InterruptionFrames fired back-to-back in one turn: only the first
    (while speaking) marks; the rest land while not-speaking and are ignored —
    no double-marking, no clobber to [NOT HEARD]."""
    d = _make_dispatcher()
    d.history.append({"role": "assistant", "content": "Your appointment is on Monday at nine."})
    obs = _observer(d)

    await obs.process_frame(BotStartedSpeakingFrame(), _DOWN)
    await obs.process_frame(_tts("Your appointment is on"), _DOWN)
    await obs.process_frame(InterruptionFrame(), _DOWN)
    await obs.process_frame(InterruptionFrame(), _DOWN)
    await obs.process_frame(InterruptionFrame(), _DOWN)

    content = str(d.history[-1]["content"])
    assert content == _expected("Your appointment is on")
    assert content.count(_MARK) == 1
    assert "[NOT HEARD]" not in content


async def test_interrupt_before_any_audio_marks_not_heard() -> None:
    """Caller talks the instant the bot starts (no TTS emitted yet) → [NOT HEARD]."""
    d = _make_dispatcher()
    d.history.append({"role": "assistant", "content": "Let me check that for you."})
    obs = _observer(d)

    await obs.process_frame(BotStartedSpeakingFrame(), _DOWN)
    await obs.process_frame(InterruptionFrame(), _DOWN)

    assert d.history[-1]["content"] == f"[NOT HEARD]{_MARK}"


async def test_many_spurious_interrupts_between_turns_never_clobber() -> None:
    """A burst of InterruptionFrames while the bot is NOT speaking (e.g. line
    noise, coughs between turns) must never overwrite a fully-spoken turn."""
    d = _make_dispatcher()
    d.history.append({"role": "assistant", "content": "You're all set — goodbye!"})
    obs = _observer(d)

    # Bot finished a turn cleanly first.
    await obs.process_frame(BotStartedSpeakingFrame(), _DOWN)
    await obs.process_frame(_tts("You're all set — goodbye!"), _DOWN)
    await obs.process_frame(BotStoppedSpeakingFrame(), _DOWN)
    # Now a burst of stray interrupts with no active turn.
    for _ in range(8):
        await obs.process_frame(InterruptionFrame(), _DOWN)

    assert d.history[-1]["content"] == "You're all set — goodbye!"
    assert _MARK not in str(d.history[-1]["content"])


# ---------------------------------------------------------------------------
# DispatcherProcessor side: the caller's barge-in WORDS must survive
# ---------------------------------------------------------------------------


def _mock_dispatcher() -> MagicMock:
    d = MagicMock()
    d.state = State.BOOK_FLOW
    d.handle_user_turn = AsyncMock(return_value="ok")
    d.timing = MagicMock()
    return d


def _make_processor(d: Any) -> DispatcherProcessor:
    proc = DispatcherProcessor(d)
    proc.push_frame = AsyncMock()
    proc._agg_window_s = 0.05
    return proc


async def test_interrupt_then_choppy_bargein_becomes_one_turn() -> None:
    """Caller barges in and says a choppy multi-fragment utterance: the interrupt
    frame in the middle must NOT drop or split it — it flushes as ONE turn."""
    d = _mock_dispatcher()
    proc = _make_processor(d)

    await proc.process_frame(InterruptionFrame(), _DOWN)
    await proc.process_frame(TranscriptionFrame("no wait", "u", "t"), _DOWN)
    await proc.process_frame(InterruptionFrame(), _DOWN)
    await proc.process_frame(TranscriptionFrame("make it", "u", "t"), _DOWN)
    await proc.process_frame(TranscriptionFrame("Tuesday instead", "u", "t"), _DOWN)
    assert proc._agg_task is not None
    await proc._agg_task

    d.handle_user_turn.assert_awaited_once()
    (joined,), _ = d.handle_user_turn.await_args
    assert joined == "no wait make it Tuesday instead"


async def test_interrupt_then_silence_fires_no_turn() -> None:
    """Caller barges in (stops the bot) but says nothing intelligible — no
    TranscriptionFrame follows → no phantom user turn is dispatched."""
    d = _mock_dispatcher()
    proc = _make_processor(d)

    await proc.process_frame(InterruptionFrame(), _DOWN)
    # No TranscriptionFrame at all → nothing buffered, no flush scheduled.
    assert proc._agg_task is None
    d.handle_user_turn.assert_not_awaited()
