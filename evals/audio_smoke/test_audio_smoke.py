"""Audio smoke tests — real acoustic TTS→STT round-trip via ElevenLabs.

This is the Tier-3 audio layer that text evals and the offline frame test
(`tests/test_barge_in_pipeline.py`) cannot cover: it actually **synthesises
speech and transcribes it back**, catching the bug class where a turn is correct
as text but breaks acoustically — e.g. the agent says "nine in the morning" but
STT hears "evening", or a caller's intent is mangled by TTS artefacts.

It uses the same vendor (ElevenLabs) and voice the live bot uses, hitting the
REST endpoints directly (the bot's realtime WebSocket STT is awkward to drive
standalone; the REST `scribe_v1` model is the same acoustic engine for a smoke
check). The caller side is synthesised as if a human spoke; the bot side checks
its own spoken output stays intelligible.

Marked ``@pytest.mark.audio`` and skipped unless BOTH ``ELEVENLABS_API_KEY`` and
``PROSPER_AUDIO_LIVE=1`` are set. The double gate is deliberate: the key lives in
``.env`` (auto-loaded), so a key-only gate would make a bare ``pytest`` spend
ElevenLabs credits silently. The explicit ``PROSPER_AUDIO_LIVE`` opt-in (parallel
to ``PROSPER_EVAL_LIVE``) means only a deliberate run pays. ``make verify`` /
``make test`` / pre-commit all scope to ``tests/`` and never collect this. Run it:

    make audio-smoke
    # or, without make (PowerShell):
    #   $env:PROSPER_AUDIO_LIVE=1; uv run pytest evals/audio_smoke -v

The full loop's remaining piece — feeding synthesised caller audio through the
bot's *live* Silero-VAD + realtime-STT pipeline and an LLM judge — stays deferred
(needs the WebSocket transport + a judge pass); see ``FUTURE.md`` §2.4.
"""

from __future__ import annotations

import os
import re

import httpx
import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.audio

# Load .env so a developer with a key in .env can just run the suite.
load_dotenv()

_API = "https://api.elevenlabs.io/v1"
# Same voice + TTS model the live bot uses (src/prosper/bot.py).
_VOICE_ID = "SAz9YHcvj6GT2YYXdXww"
_TTS_MODEL = "eleven_flash_v2_5"
_STT_MODEL = "scribe_v1"

_ENABLED = (
    bool(os.environ.get("ELEVENLABS_API_KEY")) and os.environ.get("PROSPER_AUDIO_LIVE") == "1"
)
_skip = pytest.mark.skipif(
    not _ENABLED,
    reason="audio smoke requires ELEVENLABS_API_KEY + PROSPER_AUDIO_LIVE=1 (live credits)",
)


def _synthesize(text: str) -> bytes:
    """Render ``text`` to speech audio (mp3) via ElevenLabs TTS — the bot's voice."""
    resp = httpx.post(
        f"{_API}/text-to-speech/{_VOICE_ID}",
        headers={
            "xi-api-key": os.environ["ELEVENLABS_API_KEY"],
            "content-type": "application/json",
        },
        json={"text": text, "model_id": _TTS_MODEL},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.content


def _transcribe(audio: bytes) -> str:
    """Transcribe ``audio`` back to text via ElevenLabs STT (scribe)."""
    resp = httpx.post(
        f"{_API}/speech-to-text",
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
        data={"model_id": _STT_MODEL},
        files={"file": ("clip.mp3", audio, "audio/mpeg")},
        timeout=120.0,
    )
    resp.raise_for_status()
    text = resp.json()["text"]
    return str(text)


def _norm(s: str) -> str:
    """Lowercase, strip punctuation to spaces, collapse — for tolerant matching."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s.lower())).strip()


def _roundtrip(text: str) -> str:
    """Speak ``text`` and hear it back — the acoustic round-trip under test."""
    return _norm(_transcribe(_synthesize(text)))


@_skip
def test_caller_intent_survives_roundtrip() -> None:
    """A caller's booking intent must survive TTS→STT — the words the bot acts on."""
    heard = _roundtrip("I would like to book an appointment with a therapist.")
    for token in ("book", "appointment", "therapist"):
        assert token in heard, f"{token!r} lost in acoustic round-trip: {heard!r}"


@_skip
def test_time_of_day_is_not_flipped() -> None:
    """The classic voice bug: 'morning' heard as 'evening'/'afternoon'. A flipped
    time books the wrong slot. Assert the time-of-day word survives intact."""
    heard = _roundtrip("Your appointment is confirmed for Monday at nine in the morning.")
    assert "morning" in heard, f"time-of-day lost: {heard!r}"
    assert "evening" not in heard and "afternoon" not in heard, f"time-of-day flipped: {heard!r}"
    assert "monday" in heard, f"day lost: {heard!r}"


@_skip
def test_bot_reply_stays_intelligible() -> None:
    """The bot's own spoken output (incl. a patient name) must transcribe back
    cleanly — guards against TTS artefacts garbling a confirmation."""
    heard = _roundtrip("You're all set, Ada. We'll see you on Wednesday.")
    assert "ada" in heard, f"name garbled: {heard!r}"
    assert "set" in heard, f"confirmation garbled: {heard!r}"
    assert "wednesday" in heard, f"day garbled: {heard!r}"
