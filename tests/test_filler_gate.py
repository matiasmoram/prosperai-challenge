"""Tests for the latency-gated filler emission in ``bot._should_emit_filler``.

Earlier revisions pushed a per-state filler unconditionally before every
dispatcher turn. That made the bot say "One moment. Looking you up — this
can take a few seconds." in front of a 30 ms local SQLite lookup. The
gate added in 2026-05-23 predicts the next turn's latency from
``dispatcher.timing.summary()`` and only emits if the prediction exceeds
``FILLER_LATENCY_THRESHOLD_MS``. These tests pin that behavior.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

# pipecat import is expensive; set required env BEFORE import so the
# bot module's fail-fast guard doesn't SystemExit on this test process.
os.environ.setdefault("ELEVENLABS_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")

from pipecat.frames.frames import TranscriptionFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection

from prosper.bot import (
    DEFAULT_TOOL_LATENCY_MS,
    FILLER_LATENCY_THRESHOLD_MS,
    LLM_BASELINE_LATENCY_MS,
    DispatcherProcessor,
    _should_emit_filler,
)
from prosper.flows import State


def _make_processor(dispatcher: object) -> DispatcherProcessor:
    proc = DispatcherProcessor(dispatcher)  # type: ignore[arg-type]
    proc.push_frame = AsyncMock()  # type: ignore[method-assign]
    # Transcript turns are debounced (fragments aggregate, flush after a quiet
    # gap); a tiny window keeps these integration tests fast.
    proc._agg_window_s = 0.05
    return proc


async def _feed(proc: DispatcherProcessor, text: str) -> None:
    """Feed one transcript fragment, then await the debounced flush."""
    await proc.process_frame(
        TranscriptionFrame(text=text, user_id="u", timestamp="t"), FrameDirection.DOWNSTREAM
    )
    task = proc._agg_task
    if task is not None:
        await task


def _dispatcher_with_summary(state: State, summary: dict[str, dict[str, float]]) -> MagicMock:
    """Build a mock dispatcher whose timing.summary() returns ``summary``."""
    d = MagicMock()
    d.state = state
    d.handle_user_turn = AsyncMock(return_value="ack")
    d.timing = MagicMock()
    d.timing.summary = MagicMock(return_value=summary)
    return d


def test_should_emit_filler_false_for_state_with_no_tools() -> None:
    """GREETING / CHOOSE_INTENT / END do not fire tools — always silent."""
    d = _dispatcher_with_summary(State.CHOOSE_INTENT, {})
    assert _should_emit_filler(d, State.CHOOSE_INTENT) is False
    d = _dispatcher_with_summary(State.GREETING, {})
    assert _should_emit_filler(d, State.GREETING) is False
    d = _dispatcher_with_summary(State.END, {})
    assert _should_emit_filler(d, State.END) is False


def test_should_emit_filler_true_on_cold_start() -> None:
    """No timing history → fall back to DEFAULT_TOOL_LATENCY_MS, expect emit."""
    # IDENTIFY_PATIENT has two tools; with no history each defaults to 500 ms
    # → predicted = 300 (LLM) + 500 (worst) = 800 ms >= 700 ms threshold.
    d = _dispatcher_with_summary(State.IDENTIFY_PATIENT, {})
    assert _should_emit_filler(d, State.IDENTIFY_PATIENT) is True


def test_should_emit_filler_false_when_warm_and_fast() -> None:
    """Real p95 of ~30 ms for local SQLite lookup → predicted 330 ms → silent."""
    summary = {
        "tool:find_patient_by_phone": {"count": 12, "p50": 18, "p95": 30, "max": 45},
        "tool:find_patient_by_name_dob": {"count": 12, "p50": 22, "p95": 35, "max": 60},
    }
    d = _dispatcher_with_summary(State.IDENTIFY_PATIENT, summary)
    # 300 + max(30, 35) = 335 ms < 700 ms → silent
    assert _should_emit_filler(d, State.IDENTIFY_PATIENT) is False


def test_should_emit_filler_true_when_warm_and_slow() -> None:
    """Real p95 exceeds threshold → predicted > 700 ms → emit."""
    summary = {
        "tool:list_availability_slots": {"count": 3, "p50": 800, "p95": 1500, "max": 2200},
    }
    d = _dispatcher_with_summary(State.BOOK_FLOW, summary)
    # 300 + 1500 = 1800 ms >= 700 ms → emit
    assert _should_emit_filler(d, State.BOOK_FLOW) is True


def test_should_emit_filler_uses_worst_tool_p95() -> None:
    """Among whitelisted tools, prediction uses the slowest p95."""
    summary = {
        "tool:get_upcoming_appointments": {"count": 5, "p50": 20, "p95": 40, "max": 80},
        "tool:list_availability_slots": {"count": 5, "p50": 250, "p95": 600, "max": 900},
    }
    d = _dispatcher_with_summary(State.RESCHEDULE_FLOW, summary)
    # max(40, 600) = 600 → 300 + 600 = 900 ms ≥ 700 ms → emit
    assert _should_emit_filler(d, State.RESCHEDULE_FLOW) is True


def test_should_emit_filler_defensive_on_non_dict_summary() -> None:
    """A stubbed timing collector that returns a MagicMock falls through to emit."""
    d = MagicMock()
    d.state = State.IDENTIFY_PATIENT
    d.timing = MagicMock()  # summary() returns a MagicMock, not a dict
    assert _should_emit_filler(d, State.IDENTIFY_PATIENT) is True


def test_constants_are_consistent() -> None:
    """Cold-start default must trip the threshold or the gate never fires fresh."""
    assert LLM_BASELINE_LATENCY_MS + DEFAULT_TOOL_LATENCY_MS >= FILLER_LATENCY_THRESHOLD_MS


async def test_processor_suppresses_filler_in_warm_fast_state() -> None:
    """Integration: warm IDENTIFY_PATIENT lookup should not push a filler frame."""
    fast_summary = {
        "tool:find_patient_by_phone": {"count": 20, "p50": 12, "p95": 28, "max": 50},
        "tool:find_patient_by_name_dob": {"count": 20, "p50": 18, "p95": 32, "max": 70},
    }
    d = _dispatcher_with_summary(State.IDENTIFY_PATIENT, fast_summary)
    proc = _make_processor(d)
    await _feed(proc, "my phone is 2025550100")

    pushed_texts = [
        call.args[0].text
        for call in proc.push_frame.call_args_list
        if isinstance(call.args[0], TTSSpeakFrame)
    ]
    # The dispatcher reply ("ack") still gets pushed; the filler does not.
    assert not any(t.startswith("One moment.") for t in pushed_texts)
    assert "ack" in pushed_texts


async def test_processor_emits_filler_in_warm_slow_state() -> None:
    """Integration: warm BOOK_FLOW with slow availability call pushes the filler."""
    slow_summary = {
        "tool:list_availability_slots": {"count": 4, "p50": 600, "p95": 1200, "max": 1800},
    }
    d = _dispatcher_with_summary(State.BOOK_FLOW, slow_summary)
    proc = _make_processor(d)
    await _feed(proc, "any time tuesday")

    pushed_texts = [
        call.args[0].text
        for call in proc.push_frame.call_args_list
        if isinstance(call.args[0], TTSSpeakFrame)
    ]
    assert any("check what's available" in t for t in pushed_texts)
