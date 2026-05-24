"""Unit tests for the CHOOSE_INTENT appointment prefetch (Wave 2).

Covers:
- Flag set on success; list populated with correct shape.
- Guard fires at most once per call even across multiple _llm_turn calls.
- EHR error leaves flag False and list empty (safe fallback to normal UX).
- _messages_for_llm injects the zero-appointments note (choose_ctx=False)
  and leaves the message unchanged for has-appointments (True) and unknown (None).
- Prefetch populates last_upcoming_appointments with the right shape for the
  existing CANCEL/RESCHEDULE_FLOW validators (id, start_at, provider_name).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from prosper.dispatcher import Dispatcher, LLMClientProtocol, LLMReply
from prosper.ehr_client import EHRClient
from prosper.flows import State


class _NopLLM(LLMClientProtocol):
    """Returns a single canned text reply, no tool calls."""

    def __init__(self, text: str = "How can I help?") -> None:
        self._text = text

    async def generate(self, *, state: str, history: list[dict], tools: list[dict]) -> LLMReply:
        return LLMReply(text=self._text, tool_calls=[])


def _make_dispatcher(ehr_client: EHRClient, *, text: str = "How can I help?") -> Dispatcher:
    d = Dispatcher(llm=_NopLLM(text), ehr_client=ehr_client)
    d.state = State.CHOOSE_INTENT
    d.memory.identified_patient = {"id": "patient-uuid-1", "first_name": "Ada", "last_name": "L"}
    return d


# ---------------------------------------------------------------------------
# Prefetch behaviour
# ---------------------------------------------------------------------------


async def test_prefetch_sets_flag_and_populates_list_when_appointments_exist(
    ehr_client: EHRClient,
) -> None:
    """Prefetch succeeds → flag True, list populated with correct shape."""
    appts = [
        {
            "id": "appt-1",
            "start_at": "2026-06-01T10:00",
            "provider_name": "Dr. Patel",
            "specialty": "General Practice",
        }
    ]
    with patch.object(ehr_client, "get_upcoming_appointments", new=AsyncMock(return_value=appts)):
        d = _make_dispatcher(ehr_client)
        await d._prefetch_upcoming_on_choose_intent()

    assert d.memory.upcoming_appointments_prefetched is True
    assert len(d.memory.last_upcoming_appointments) == 1
    rec = d.memory.last_upcoming_appointments[0]
    assert rec["id"] == "appt-1"
    assert rec["start_at"] == "2026-06-01T10:00"
    assert rec["provider_name"] == "Dr. Patel"
    # Only the three keys the dispatcher cares about should be present.
    assert set(rec.keys()) == {"id", "start_at", "provider_name"}


async def test_prefetch_sets_flag_and_empty_list_when_no_appointments(
    ehr_client: EHRClient,
) -> None:
    """Prefetch succeeds with empty list → flag True, list stays []."""
    with patch.object(ehr_client, "get_upcoming_appointments", new=AsyncMock(return_value=[])):
        d = _make_dispatcher(ehr_client)
        await d._prefetch_upcoming_on_choose_intent()

    assert d.memory.upcoming_appointments_prefetched is True
    assert d.memory.last_upcoming_appointments == []


async def test_prefetch_ehr_error_leaves_flag_false(ehr_client: EHRClient) -> None:
    """EHR error → flag stays False, list stays [] (safe fallback to normal UX)."""

    async def _boom(patient_id: str) -> list[dict]:
        raise RuntimeError("connection refused")

    with patch.object(ehr_client, "get_upcoming_appointments", new=_boom):
        d = _make_dispatcher(ehr_client)
        await d._prefetch_upcoming_on_choose_intent()

    assert d.memory.upcoming_appointments_prefetched is False
    assert d.memory.last_upcoming_appointments == []


async def test_prefetch_no_op_without_identified_patient(ehr_client: EHRClient) -> None:
    """No identified patient → prefetch is a no-op (no EHR call, flag stays False)."""
    mock_get = AsyncMock(return_value=[])
    with patch.object(ehr_client, "get_upcoming_appointments", new=mock_get):
        d = _make_dispatcher(ehr_client)
        d.memory.identified_patient = None
        await d._prefetch_upcoming_on_choose_intent()

    mock_get.assert_not_called()
    assert d.memory.upcoming_appointments_prefetched is False


# ---------------------------------------------------------------------------
# Guard fires exactly once per call (idempotency)
# ---------------------------------------------------------------------------


async def test_guard_fires_ehr_call_only_once_across_multiple_llm_turns(
    ehr_client: EHRClient,
) -> None:
    """The _llm_turn guard runs only when flag is False → EHR called at most once."""
    call_count = 0

    async def _count(patient_id: str) -> list[dict]:
        nonlocal call_count
        call_count += 1
        return []

    with patch.object(ehr_client, "get_upcoming_appointments", new=_count):
        d = _make_dispatcher(ehr_client)
        async with ehr_client:
            # First turn in CHOOSE_INTENT — should trigger prefetch.
            await d._llm_turn()
            # Second turn in CHOOSE_INTENT — flag now True, no repeat fetch.
            await d._llm_turn()

    assert call_count == 1


async def test_guard_does_not_fire_outside_choose_intent(ehr_client: EHRClient) -> None:
    """Prefetch guard only fires in CHOOSE_INTENT, not in other states."""
    mock_get = AsyncMock(return_value=[])
    with patch.object(ehr_client, "get_upcoming_appointments", new=mock_get):
        d = _make_dispatcher(ehr_client)
        d.state = State.BOOK_FLOW  # different state
        async with ehr_client:
            await d._llm_turn()

    mock_get.assert_not_called()
    assert d.memory.upcoming_appointments_prefetched is False


# ---------------------------------------------------------------------------
# _messages_for_llm hint injection
# ---------------------------------------------------------------------------


def _task_content(d: Dispatcher) -> str:
    """Extract the CHOOSE_INTENT task message system content from _messages_for_llm."""
    msgs = d._messages_for_llm()
    # Second system message is the task message (first is CLINIC_PERSONA).
    task_msgs = [m for m in msgs if m.get("role") == "system"]
    assert len(task_msgs) >= 2
    return str(task_msgs[1]["content"])


def test_messages_for_llm_unknown_ctx_no_injection(ehr_client: EHRClient) -> None:
    """choose_ctx=None (prefetch not done) → no appointment note injected."""
    d = _make_dispatcher(ehr_client)
    # Flag False → choose_ctx = None → no injection
    assert d.memory.upcoming_appointments_prefetched is False
    content = _task_content(d)
    assert "Do NOT offer cancel or reschedule" not in content


def test_messages_for_llm_has_appointments_no_injection(ehr_client: EHRClient) -> None:
    """choose_ctx=True (has appointments) → no appointment note injected."""
    d = _make_dispatcher(ehr_client)
    d.memory.upcoming_appointments_prefetched = True
    d.memory.last_upcoming_appointments = [
        {"id": "a1", "start_at": "2026-06-01T10:00", "provider_name": "Dr. P"}
    ]
    content = _task_content(d)
    assert "Do NOT offer cancel or reschedule" not in content


def test_messages_for_llm_no_appointments_injects_note(ehr_client: EHRClient) -> None:
    """choose_ctx=False (zero appointments confirmed) → note injected in task message."""
    d = _make_dispatcher(ehr_client)
    d.memory.upcoming_appointments_prefetched = True
    d.memory.last_upcoming_appointments = []  # confirmed zero
    content = _task_content(d)
    # F3's exact wording: "no upcoming appointments" + "Do NOT offer cancel or reschedule"
    assert "no upcoming appointments" in content.lower()
    assert "Do NOT offer cancel or reschedule" in content
