"""Front-desk mail is written for the full appointment lifecycle.

Booking already emitted a ``booking_confirmation`` record.
These tests pin the two added lifecycle events: a ``cancellation`` notice
after an Ok cancel and a ``reschedule`` notice after an Ok reschedule, so the
front-desk inbox reflects freed/moved slots — not just new bookings.

The emitters are off-spine, fire-and-forget side-effects (no tool, no FSM
edit). As in ``test_llm_failure.py`` we drain ``_inflight_publishes`` before
reading the store, because the write task is detached via ``create_task``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from prosper.dispatcher import Dispatcher, LLMClientProtocol, LLMReply
from prosper.ehr_client import EHRClient
from prosper.integrations.mail import MailStore


class _SilentLLM(LLMClientProtocol):
    """Never invoked here — these tests call the emitters directly."""

    async def generate(
        self, *, state: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMReply:
        return LLMReply(text="", tool_calls=[])


def _identified(d: Dispatcher) -> None:
    d.memory.identified_patient = {
        "id": "pat-1",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "phone": "2025550100",
    }


async def _drain(d: Dispatcher) -> None:
    if d._inflight_publishes:
        await asyncio.gather(*list(d._inflight_publishes))


async def test_cancellation_writes_mail_with_provider_from_memory(
    seeded_ehr_client: EHRClient, tmp_path: Path
) -> None:
    """Cancel recovers provider + start from last_upcoming_appointments
    (cancel_appointment only returns {ok, appointment_id})."""
    store = MailStore(root=tmp_path / "mail")
    async with seeded_ehr_client:
        d = Dispatcher(llm=_SilentLLM(), ehr_client=seeded_ehr_client, mail_store=store)
        _identified(d)
        d.memory.last_upcoming_appointments = [
            {"id": "appt-9", "start_at": "2026-06-01 14:00", "provider_name": "Dr. Patel"}
        ]
        d._emit_cancellation_notice({"ok": True, "appointment_id": "appt-9"})
        await _drain(d)

    msgs = store.list_messages()
    assert len(msgs) == 1
    m = msgs[0]
    assert m.kind == "cancellation"
    assert m.to_label == "Dr. Patel"
    assert "Ada Lovelace" in m.body and "cancelled" in m.body
    assert "2026-06-01 14:00" in m.subject


async def test_reschedule_writes_mail_with_new_slot(
    seeded_ehr_client: EHRClient, tmp_path: Path
) -> None:
    """Reschedule result carries the NEW start_at + provider_name (like a booking)."""
    store = MailStore(root=tmp_path / "mail")
    async with seeded_ehr_client:
        d = Dispatcher(llm=_SilentLLM(), ehr_client=seeded_ehr_client, mail_store=store)
        _identified(d)
        d._emit_reschedule_notice(
            {
                "appointment_id": "appt-9",
                "start_at": "2026-06-02 09:30",
                "provider_name": "Dr. Singh",
            }
        )
        await _drain(d)

    msgs = store.list_messages()
    assert len(msgs) == 1
    m = msgs[0]
    assert m.kind == "reschedule"
    assert m.to_label == "Dr. Singh"
    assert "moved" in m.body and "2026-06-02 09:30" in m.body


async def test_lifecycle_emitters_are_noop_without_store(
    seeded_ehr_client: EHRClient,
) -> None:
    """No MailStore injected → emitters must not raise (tests/evals path)."""
    async with seeded_ehr_client:
        d = Dispatcher(llm=_SilentLLM(), ehr_client=seeded_ehr_client, mail_store=None)
        _identified(d)
        # Neither must schedule a task nor raise.
        d._emit_cancellation_notice({"ok": True, "appointment_id": "x"})
        d._emit_reschedule_notice({"appointment_id": "x", "start_at": "", "provider_name": ""})
    assert not d._inflight_publishes


async def test_cancellation_unknown_id_still_emits_generic(
    seeded_ehr_client: EHRClient, tmp_path: Path
) -> None:
    """If the cancelled id isn't in memory, the record still goes out with a
    generic provider rather than being dropped (defensive)."""
    store = MailStore(root=tmp_path / "mail")
    async with seeded_ehr_client:
        d = Dispatcher(llm=_SilentLLM(), ehr_client=seeded_ehr_client, mail_store=store)
        _identified(d)
        d.memory.last_upcoming_appointments = []
        d._emit_cancellation_notice({"ok": True, "appointment_id": "missing"})
        await _drain(d)

    msgs = store.list_messages()
    assert len(msgs) == 1
    assert msgs[0].kind == "cancellation"
    assert msgs[0].to_label == "your provider"
