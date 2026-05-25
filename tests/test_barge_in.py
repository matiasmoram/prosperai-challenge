"""Unit tests for barge-in / voice-overlap history-truncation logic (Wave 7, N-002).

Tests the ``mark_last_assistant_interrupted`` method on the Dispatcher, which
is the only portion of barge-in handling that can be exercised without live
Pipecat audio frames. The real-time pipeline wiring (TTSAudibleObserver +
InterruptionFrame propagation) requires live audio in CI and is verified by
the human operator in the staging environment (see Wave 7 report).

Scenarios covered:
  N-002-a: partial spoken text → history[-1] truncated + marker appended.
  N-002-b: empty spoken text (bot barely started) → [NOT HEARD] prefix.
  N-002-c: idempotency — calling twice does not double-apply the marker.
  N-002-d: no-op when history is empty.
  N-002-e: no-op when history[-1] is a user turn, not an assistant turn.
"""

from __future__ import annotations

import httpx

from prosper.dispatcher import Dispatcher, LLMClientProtocol, LLMReply
from prosper.ehr_client import EHRClient


class _NoOpLLM(LLMClientProtocol):
    """Minimal LLM stub — tests call mark_last_assistant_interrupted directly."""

    async def generate(
        self,
        *,
        state: str,
        history: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> LLMReply:
        return LLMReply(text="")


def _make_dispatcher() -> Dispatcher:
    client = EHRClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        base_url="http://ehr-test",
    )
    return Dispatcher(
        llm=_NoOpLLM(),
        ehr_client=client,
        session_id="test-barge-in",
    )


# ---------------------------------------------------------------------------
# N-002-a: partial spoken text truncates and marks
# ---------------------------------------------------------------------------


def test_mark_interrupted_partial_text() -> None:
    """N-002-a: a partial spoken text truncates history[-1] and appends marker."""
    d = _make_dispatcher()
    # Seed history with a full assistant reply
    full_reply = (
        "Here are three slots for you: Monday at 9am, Tuesday at 10am, or Wednesday at 2pm."
    )
    d.history.append({"role": "assistant", "content": full_reply})

    spoken = "Here are three slots for you: Monday at 9am"
    d.mark_last_assistant_interrupted(spoken)

    last = d.history[-1]
    assert last["role"] == "assistant"
    content = str(last["content"])
    assert content == f"{spoken}… [INTERRUPTED by user]"
    assert "Tuesday" not in content
    assert "Wednesday" not in content


# ---------------------------------------------------------------------------
# N-002-b: empty spoken text uses [NOT HEARD] prefix
# ---------------------------------------------------------------------------


def test_mark_interrupted_empty_spoken_text() -> None:
    """N-002-b: if TTS buffer was empty (bot cut off before emitting), use [NOT HEARD]."""
    d = _make_dispatcher()
    d.history.append({"role": "assistant", "content": "Let me check that for you."})

    d.mark_last_assistant_interrupted("")

    content = str(d.history[-1]["content"])
    assert content == "[NOT HEARD] [INTERRUPTED by user]"


def test_mark_interrupted_whitespace_only_spoken_text() -> None:
    """N-002-b variant: whitespace-only spoken_text treated as empty → [NOT HEARD]."""
    d = _make_dispatcher()
    d.history.append({"role": "assistant", "content": "Let me check that for you."})

    d.mark_last_assistant_interrupted("   ")

    content = str(d.history[-1]["content"])
    assert content == "[NOT HEARD] [INTERRUPTED by user]"


# ---------------------------------------------------------------------------
# N-002-c: idempotency — second call is a no-op
# ---------------------------------------------------------------------------


def test_mark_interrupted_idempotent() -> None:
    """N-002-c: calling mark_last_assistant_interrupted twice does not double-mark."""
    d = _make_dispatcher()
    d.history.append({"role": "assistant", "content": "I found a slot on Monday."})

    spoken = "I found a slot"
    d.mark_last_assistant_interrupted(spoken)
    content_after_first = str(d.history[-1]["content"])

    # Second call with different text — must not modify again
    d.mark_last_assistant_interrupted("I found a slot on Monday.")
    content_after_second = str(d.history[-1]["content"])

    assert content_after_first == content_after_second
    assert content_after_first.count("[INTERRUPTED by user]") == 1


# ---------------------------------------------------------------------------
# N-002-d: no-op on empty history
# ---------------------------------------------------------------------------


def test_mark_interrupted_empty_history() -> None:
    """N-002-d: empty history → no exception, no side effect."""
    d = _make_dispatcher()
    assert d.history == []  # confirm empty

    d.mark_last_assistant_interrupted("anything")  # must not raise

    assert d.history == []


# ---------------------------------------------------------------------------
# N-002-e: no-op when last turn is user, not assistant
# ---------------------------------------------------------------------------


def test_mark_interrupted_last_is_user_turn() -> None:
    """N-002-e: if history[-1] is a user turn, leave it untouched."""
    d = _make_dispatcher()
    d.history.append({"role": "assistant", "content": "Welcome to Prosper Health."})
    d.history.append({"role": "user", "content": "I need to book an appointment."})

    d.mark_last_assistant_interrupted("Welcome to")

    # User turn must be unchanged
    assert d.history[-1]["role"] == "user"
    assert d.history[-1]["content"] == "I need to book an appointment."
    # Assistant turn before it must also be unchanged (no marker propagation)
    assert "[INTERRUPTED by user]" not in str(d.history[-2]["content"])
