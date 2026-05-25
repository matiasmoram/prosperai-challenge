"""Unit tests for the LLM-total-failure reliability path (Wave 6, req 4).

Simulates the case where the LLM call itself raises (tenacity retries +
fallback model both exhausted). Asserts:
  1. handle_user_turn does NOT propagate the exception.
  2. The bot speaks FALLBACK_LINES["system_failure"] (canned — no LLM needed).
  3. If a MailStore is injected, a bot_failed mail is written.
  4. The transcript records a ``llm_total_failure`` entry.

Note on M-001: mock-eval can't simulate live LLM failure because
``MockDispatcherLLM`` is scripted and never raises. These tests use a
dedicated ``FailingLLM`` adapter that always raises ``APIConnectionError``
so the dispatcher's catch is exercised directly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from prosper.dispatcher import Dispatcher, LLMClientProtocol, LLMReply
from prosper.ehr_client import EHRClient
from prosper.integrations.mail import MailStore
from prosper.prompts import FALLBACK_LINES


class FailingLLM(LLMClientProtocol):
    """Adapter that always raises — simulates tenacity + fallback exhausted."""

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc or RuntimeError("simulated total LLM failure")

    async def generate(
        self, *, state: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMReply:
        raise self._exc


class GreetingThenFailingLLM(LLMClientProtocol):
    """Returns one canned greeting reply, then raises on every subsequent call.

    Lets the dispatcher complete start() (GREETING turn) without error so we
    can exercise the failure path during handle_user_turn.
    """

    def __init__(self) -> None:
        self._calls = 0

    async def generate(
        self, *, state: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMReply:
        self._calls += 1
        if self._calls == 1:
            return LLMReply(text="Hi, you've reached Prosper Health!", tool_calls=[])
        raise RuntimeError("simulated total LLM failure after greeting")


async def test_llm_total_failure_does_not_raise(ehr_client: EHRClient) -> None:
    """handle_user_turn must swallow LLM exceptions — never crash the WebRTC session."""
    llm = GreetingThenFailingLLM()
    async with ehr_client:
        d = Dispatcher(llm=llm, ehr_client=ehr_client)
        await d.start()
        # Must not raise even though every LLM call from here raises.
        reply = await d.handle_user_turn("hello, I want to book")
    # A reply was returned (not None, not empty string).
    assert reply


async def test_llm_total_failure_speaks_canned_line(ehr_client: EHRClient) -> None:
    """Bot speaks FALLBACK_LINES['system_failure'] on total LLM failure."""
    llm = GreetingThenFailingLLM()
    async with ehr_client:
        d = Dispatcher(llm=llm, ehr_client=ehr_client)
        await d.start()
        reply = await d.handle_user_turn("hello")
    assert reply == FALLBACK_LINES["system_failure"]


async def test_llm_total_failure_records_transcript_entry(ehr_client: EHRClient) -> None:
    """Dispatcher records a ``llm_total_failure`` transcript entry."""
    llm = GreetingThenFailingLLM()
    async with ehr_client:
        d = Dispatcher(llm=llm, ehr_client=ehr_client)
        await d.start()
        await d.handle_user_turn("hello")
    kinds = [e["kind"] for e in d.transcript]
    assert "llm_total_failure" in kinds


async def test_llm_total_failure_fires_bot_failed_mail(
    ehr_client: EHRClient, tmp_path: Path
) -> None:
    """A bot_failed mail is written to MailStore on total LLM failure."""
    mail_root = tmp_path / "mail"
    mail_store = MailStore(root=mail_root)
    llm = GreetingThenFailingLLM()
    async with ehr_client:
        d = Dispatcher(llm=llm, ehr_client=ehr_client, mail_store=mail_store)
        await d.start()
        await d.handle_user_turn("hello")
        # Drain all in-flight fire-and-forget tasks (mail write tasks are
        # created via asyncio.create_task inside _emit_system_failure_mail).
        # aiofiles uses a thread-pool executor, so we need a real sleep
        # to let the OS schedule the background thread.
        if d._inflight_publishes:
            await asyncio.gather(*list(d._inflight_publishes))

    messages = mail_store.list_messages()
    assert messages, "expected at least one mail record after LLM failure"
    assert any(m.kind == "bot_failed" for m in messages), (
        f"expected a bot_failed record; got kinds={[m.kind for m in messages]}"
    )


async def test_llm_total_failure_no_mail_when_no_store(ehr_client: EHRClient) -> None:
    """When no MailStore is injected, the failure path is still safe (no crash)."""
    llm = GreetingThenFailingLLM()
    async with ehr_client:
        d = Dispatcher(llm=llm, ehr_client=ehr_client, mail_store=None)
        await d.start()
        # Must not raise even without a mail store.
        reply = await d.handle_user_turn("hello")
    assert reply == FALLBACK_LINES["system_failure"]
