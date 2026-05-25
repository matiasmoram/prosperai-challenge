"""Tests for ``prosper.observers.TTSAudibleObserver``.

The observer captures TTS text frames between BotStartedSpeakingFrame and
the next BotStoppedSpeakingFrame (clean end → discard) or
StartInterruptionFrame (interrupt → flush to dispatcher). These tests
pin the lifecycle so regressions in frame routing don't silently drop
the interruption marker. See ``docs/research/interruption_design.md``.
"""

from __future__ import annotations

import os

# pipecat is expensive to import; preload env so bot.py's fail-fast doesn't
# fire when tests transitively import bot via DispatcherProcessor sibling modules.
os.environ.setdefault("ELEVENLABS_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")

from unittest.mock import AsyncMock

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterruptionFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from prosper.observers import TTSAudibleObserver


def _tts(text: str) -> TTSTextFrame:
    """TTSTextFrame requires an aggregation tag in pipecat 0.0.100."""
    return TTSTextFrame(text=text, aggregated_by="word")


def _make_observer() -> tuple[TTSAudibleObserver, list[str]]:
    captured: list[str] = []
    obs = TTSAudibleObserver(on_interrupt=captured.append)
    obs.push_frame = AsyncMock()  # type: ignore[method-assign]
    return obs, captured


async def test_clean_end_drops_buffer() -> None:
    """BotStopped without an interrupt → buffer cleared, on_interrupt not called."""
    obs, captured = _make_observer()
    await obs.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await obs.process_frame(_tts("Hello there"), FrameDirection.DOWNSTREAM)
    await obs.process_frame(_tts(" — how are you?"), FrameDirection.DOWNSTREAM)
    await obs.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert captured == []
    assert obs.buffer == ""


async def test_interrupt_flushes_buffered_text_to_callback() -> None:
    """Interruption → on_interrupt called with the buffered TTS text."""
    obs, captured = _make_observer()
    await obs.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await obs.process_frame(_tts("Booking 2 PM with Dr. Smi"), FrameDirection.DOWNSTREAM)
    await obs.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
    assert captured == ["Booking 2 PM with Dr. Smi"]
    assert obs.buffer == ""


async def test_interrupt_before_any_text_callback_with_empty_string() -> None:
    """Interrupt before any TTSTextFrame → callback fires with empty string
    so the dispatcher can still mark the turn as not-heard."""
    obs, captured = _make_observer()
    await obs.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await obs.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
    assert captured == [""]


async def test_interrupt_ignored_when_not_speaking() -> None:
    """An InterruptionFrame that lands while NOT speaking must be ignored.

    Stray TTSTextFrames outside a BotStarted/BotStopped window are not captured,
    and a spurious/late InterruptionFrame between turns must NOT fire the
    callback: doing so calls mark_last_assistant_interrupted("") which overwrites
    the PREVIOUS, fully-spoken assistant turn with "[NOT HEARD]"
    (dispatcher.py:579). The callback fires only when the bot is actively
    speaking (BotStarted seen) — see test_interrupt_before_any_text_… for that.
    """
    obs, captured = _make_observer()
    await obs.process_frame(_tts("stray text"), FrameDirection.DOWNSTREAM)
    await obs.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
    assert captured == []


async def test_observer_forwards_every_frame_downstream() -> None:
    """The observer is a pass-through — every received frame must be pushed
    so the transport.output downstream still gets audio/control frames.

    Note: ``super().process_frame`` does pipecat's own system-frame
    bookkeeping for ``InterruptionFrame``; without a live TaskManager
    (which a unit test lacks) that emits a spurious ErrorFrame. So we
    assert each *input* frame is forwarded rather than an exact count."""
    obs, _ = _make_observer()
    frames = [
        BotStartedSpeakingFrame(),
        _tts("hello"),
        InterruptionFrame(),
    ]
    for f in frames:
        await obs.process_frame(f, FrameDirection.DOWNSTREAM)
    pushed = [c.args[0] for c in obs.push_frame.call_args_list]
    for f in frames:
        assert f in pushed, f"{type(f).__name__} not forwarded downstream"


async def test_multiple_turns_isolated() -> None:
    """Each BotStarted resets the buffer — turn N's text doesn't bleed into turn N+1."""
    obs, captured = _make_observer()
    # Turn 1 — clean end.
    await obs.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await obs.process_frame(_tts("Turn one."), FrameDirection.DOWNSTREAM)
    await obs.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    # Turn 2 — interrupted.
    await obs.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await obs.process_frame(_tts("Turn two partial"), FrameDirection.DOWNSTREAM)
    await obs.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
    # Only turn 2 surfaces to the callback.
    assert captured == ["Turn two partial"]
