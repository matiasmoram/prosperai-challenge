"""Audio smoke test — drives the bot's full pipeline with synthetic caller audio.

Marked ``@pytest.mark.audio`` so it is excluded by default. To run:

    uv run pytest evals/audio_smoke -m audio

Requires ELEVENLABS_API_KEY and OPENAI_API_KEY. Costs ElevenLabs credits per
run — use sparingly.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.audio


@pytest.mark.skipif(
    not (os.environ.get("ELEVENLABS_API_KEY") and os.environ.get("OPENAI_API_KEY")),
    reason="audio smoke requires both ELEVENLABS_API_KEY and OPENAI_API_KEY",
)
def test_smoke_dispatcher_module_imports() -> None:
    """v1: assert the bot module imports and the dispatcher initialises.

    The full acoustic loop (synth caller → bot STT → bot logic → bot TTS →
    caller STT → judge) is a future-work item documented in ARCHITECTURE.md
    §17 / FUTURE.md §2.4. The interruption / call-cutoff behaviour (the
    high-value, most-noticeable audio-path bugs) is already covered offline by
    `tests/test_barge_in_pipeline.py` — no credits, runs in `make verify`.
    """
    from prosper.bot import _build_dispatcher

    dispatcher = _build_dispatcher()
    assert dispatcher.state.value == "GREETING"
