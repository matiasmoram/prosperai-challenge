"""Dispatcher logic — state transitions, tool whitelisting, transcript capture.

LLM calls are stubbed: each turn returns a pre-canned ``LLMReply`` so we can
assert that the dispatcher transitions correctly given known model output.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from prosper.dispatcher import Dispatcher, LLMClientProtocol, LLMReply, ToolCall
from prosper.ehr.api import create_app
from prosper.ehr.db import get_engine, init_db
from prosper.ehr.models import Provider, Slot
from prosper.ehr_client import EHRClient
from prosper.flows import State


class CannedLLM(LLMClientProtocol):
    def __init__(self, replies: list[LLMReply]) -> None:
        self._iter: Iterator[LLMReply] = iter(replies)
        self.received_states: list[str] = []
        self.received_tools_offered: list[list[str]] = []

    async def generate(self, *, state: str, history: list[dict], tools: list[dict]) -> LLMReply:
        self.received_states.append(state)
        self.received_tools_offered.append([t["function"]["name"] for t in tools])
        try:
            return next(self._iter)
        except StopIteration:
            return LLMReply(text="", tool_calls=[])


@pytest.fixture
def ehr_client(tmp_path, monkeypatch) -> EHRClient:
    monkeypatch.setenv("PROSPER_DB_URL", f"sqlite:///{tmp_path / 'ehr.db'}")
    get_engine(reset=True)
    init_db()
    app = create_app()
    with Session(get_engine()) as session:
        prov = Provider(name="Dr. Patel", timezone="UTC")
        session.add(prov)
        session.commit()
        start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        for i in range(2):
            session.add(
                Slot(
                    provider_id=prov.id,
                    start_at=start + timedelta(minutes=30 * i),
                    end_at=start + timedelta(minutes=30 * (i + 1)),
                )
            )
        session.commit()
    return EHRClient.for_asgi_app(app)


async def test_dispatcher_starts_in_greeting_and_transitions_on_first_user_turn(
    ehr_client: EHRClient,
) -> None:
    canned = CannedLLM(
        [
            LLMReply(text="Hi! Looking to book or cancel today?", tool_calls=[]),
            LLMReply(text="What's the best phone number to find you under?", tool_calls=[]),
        ]
    )
    async with ehr_client:
        d = Dispatcher(llm=canned, ehr_client=ehr_client)
        await d.start()
        assert d.state is State.GREETING
        await d.handle_user_turn("hi, want to book")
        assert d.state is State.IDENTIFY_PATIENT


async def test_dispatcher_rejects_tool_not_in_whitelist(ehr_client: EHRClient) -> None:
    canned = CannedLLM(
        [
            LLMReply(text="hi", tool_calls=[]),
            LLMReply(
                text="",
                tool_calls=[
                    ToolCall(
                        name="create_appointment",
                        arguments={"patient_id": "x", "slot_id": "y"},
                    )
                ],
            ),
            LLMReply(text="ok, give me your phone number", tool_calls=[]),
        ]
    )
    async with ehr_client:
        d = Dispatcher(llm=canned, ehr_client=ehr_client)
        await d.start()
        await d.handle_user_turn("book please")
        rejections = [e for e in d.transcript if e.get("kind") == "tool_rejected"]
        assert len(rejections) == 1
        assert rejections[0]["name"] == "create_appointment"
