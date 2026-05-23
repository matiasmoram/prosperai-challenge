"""Pipeline observers that bridge Pipecat speech-frame events to dispatcher state.

The dispatcher owns the conversation history; without help, it never
learns that a bot turn was cut short. ``TTSAudibleObserver`` watches the
speech-frame sequence in the pipeline and tells the dispatcher when an
in-flight bot turn was interrupted so the history line can be truncated
and marked. See ``docs/research/interruption_design.md`` for the design.
"""

from __future__ import annotations

from collections.abc import Callable

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class TTSAudibleObserver(FrameProcessor):
    """Accumulate the text TTS attempted to synthesize for the current bot
    turn; on interruption, hand it to the dispatcher.

    Place between the TTS service and ``transport.output`` so every
    ``TTSTextFrame`` the TTS engine emits passes through. The text we
    capture is what TTS *received* — not strictly what the speaker
    rendered to audio — but it's the most honest signal available in
    Pipecat 0.0.100 without forking the transport clock queue.

    Lifecycle:
        ``BotStartedSpeakingFrame`` → reset buffer, mark speaking.
        ``TTSTextFrame`` (while speaking) → append payload to buffer.
        ``BotStoppedSpeakingFrame`` (clean end) → discard buffer.
        ``InterruptionFrame`` → flush buffer to ``on_interrupt``.

    We match ``InterruptionFrame`` (the base class) rather than the
    deprecated ``StartInterruptionFrame`` so the observer keeps working
    across the Pipecat 0.0.100 → 1.x rename; ``StartInterruptionFrame``
    is a subclass, so live 0.0.100 barge-ins are still caught.

    ``on_interrupt`` is a sync callback (we don't want to block the
    frame loop on an async dispatcher mutation — the dispatcher's mark
    method writes a dict, which is fast).
    """

    def __init__(self, on_interrupt: Callable[[str], None]) -> None:
        super().__init__()
        self._on_interrupt = on_interrupt
        self._buffer: list[str] = []
        self._speaking = False

    @property
    def buffer(self) -> str:
        """Concatenated TTS payload since the last BotStartedSpeakingFrame."""
        return "".join(self._buffer)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._buffer.clear()
            self._speaking = True
        elif isinstance(frame, TTSTextFrame) and self._speaking:
            text = getattr(frame, "text", "")
            if text:
                self._buffer.append(text)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._buffer.clear()
            self._speaking = False
        elif isinstance(frame, InterruptionFrame):
            spoken = self.buffer
            self._buffer.clear()
            self._speaking = False
            self._on_interrupt(spoken)

        await self.push_frame(frame, direction)
