"""Live smoke test and unit tests for the autonomous simulator.

The live test is gated on ``PROSPER_EVAL_LIVE=1`` (+ a key) exactly like
``make eval`` — so ``make verify`` never spends tokens on it.  The unit tests
below (no gate) cover ``_caller_utterance_corrupt`` in isolation.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from dotenv import load_dotenv

load_dotenv()

_LIVE = bool(os.environ.get("PROSPER_EVAL_LIVE")) and bool(os.environ.get("OPENAI_API_KEY"))


# ---------------------------------------------------------------------------
# Unit tests: corrupt-run detection (_caller_utterance_corrupt)
# ---------------------------------------------------------------------------


def _transcript(*turns: tuple[str, str]) -> list[dict[str, Any]]:
    """Build a minimal transcript list from (kind, text) pairs."""
    return [{"kind": kind, "text": text} for kind, text in turns]


def test_corrupt_when_caller_emits_phone_placeholder() -> None:
    from tester.live_sim import _caller_utterance_corrupt

    transcript = _transcript(
        ("assistant", "What's your phone number?"),
        ("user", "My phone number is[PHONE]"),
    )
    assert _caller_utterance_corrupt(transcript) is True


# ---------------------------------------------------------------------------
# Unit tests: write-tool detection for the clarification audit (_write_tool_in)
# ---------------------------------------------------------------------------


def _ev(type_: str, payload: dict[str, Any]):
    from prosper.console.events import ConsoleEvent

    return ConsoleEvent(type=type_, ts=0.0, session_id="s1", payload=payload)


def test_write_tool_in_detects_successful_write() -> None:
    from tester.live_sim import _write_tool_in

    events = [
        _ev("tool_call_start", {"tool": "create_appointment", "args_redacted": {}, "call_id": "1"}),
        _ev(
            "tool_call_end",
            {"tool": "create_appointment", "call_id": "1", "outcome": "ok", "duration_ms": 3.0},
        ),
    ]
    assert _write_tool_in(events) == "create_appointment"


def test_write_tool_in_ignores_failed_or_read_tools() -> None:
    from tester.live_sim import _write_tool_in

    # A failed write does not count (no change committed)…
    failed = [
        _ev(
            "tool_call_end",
            {"tool": "create_appointment", "call_id": "1", "outcome": "err", "duration_ms": 1.0},
        )
    ]
    assert _write_tool_in(failed) is None
    # …nor does a successful READ tool.
    read = [
        _ev(
            "tool_call_end",
            {
                "tool": "get_upcoming_appointments",
                "call_id": "2",
                "outcome": "ok",
                "duration_ms": 1.0,
            },
        )
    ]
    assert _write_tool_in(read) is None


def test_corrupt_when_caller_emits_name_placeholder() -> None:
    from tester.live_sim import _caller_utterance_corrupt

    transcript = _transcript(
        ("assistant", "What's your name?"),
        ("user", "Hi, I'm [Your Name] calling about an appointment."),
    )
    assert _caller_utterance_corrupt(transcript) is True


def test_corrupt_when_caller_emits_dob_placeholder() -> None:
    from tester.live_sim import _caller_utterance_corrupt

    transcript = _transcript(
        ("assistant", "What's your date of birth?"),
        ("user", "My DOB is [Month, Day, Year]."),
    )
    assert _caller_utterance_corrupt(transcript) is True


def test_not_corrupt_when_placeholder_is_in_bot_turn_only() -> None:
    """A bot turn containing brackets (unusual but possible) must NOT flag corrupt.

    The check is caller-side only; the bot is the system under test.
    """
    from tester.live_sim import _caller_utterance_corrupt

    transcript = _transcript(
        ("assistant", "I have slot [1] at 10am and [2] at 11am — which works?"),
        ("user", "The first one, please."),
    )
    assert _caller_utterance_corrupt(transcript) is False


def test_not_corrupt_when_caller_uses_concrete_values() -> None:
    from tester.live_sim import _caller_utterance_corrupt

    transcript = _transcript(
        ("assistant", "What's your phone number?"),
        ("user", "It's 555-0142."),
        ("assistant", "And your date of birth?"),
        ("user", "March 3rd, 1948."),
    )
    assert _caller_utterance_corrupt(transcript) is False


def test_not_corrupt_when_transcript_is_empty() -> None:
    from tester.live_sim import _caller_utterance_corrupt

    assert _caller_utterance_corrupt([]) is False


def test_not_corrupt_with_numbered_list_brackets() -> None:
    """[1] / [2] in a caller turn are NOT placeholders (start with digit)."""
    from tester.live_sim import _caller_utterance_corrupt

    transcript = _transcript(
        ("user", "Option [1] please."),
    )
    assert _caller_utterance_corrupt(transcript) is False


# ---------------------------------------------------------------------------
# Live smoke test
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(not _LIVE, reason="set PROSPER_EVAL_LIVE=1 (+OPENAI_API_KEY) to run live sim")
async def test_hallucination_bait_stays_honest() -> None:
    from openai import AsyncOpenAI

    from tester.invariants import check_call
    from tester.live_sim import simulate_call
    from tester.personas import CURATED

    persona = next(p for p in CURATED if p.name == "hallucination_bait")
    result = await simulate_call(persona, client=AsyncOpenAI())
    assert result.error is None, result.error
    assert not result.corrupt, "hallucination_bait persona produced corrupt caller utterances"
    violations = check_call(result.events, result.transcript)
    assert not violations, "; ".join(str(v) for v in violations)
