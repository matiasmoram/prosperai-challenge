"""OpenAI adapter test — uses fake responses to avoid real network."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from prosper.dispatcher import LLMReply, ToolCall
from prosper.llm import OpenAILLMAdapter


def _fake_response(
    text: str,
    tool_calls: list[tuple[str, dict]] | None = None,
    *,
    cached_tokens: int = 0,
    prompt_tokens: int = 0,
) -> object:
    tcs = []
    for name, args in tool_calls or []:
        fn = SimpleNamespace(name=name, arguments=json.dumps(args))
        tcs.append(SimpleNamespace(id="call_x", function=fn))
    msg = SimpleNamespace(content=text, tool_calls=tcs or None)
    choice = SimpleNamespace(message=msg)
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=0,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
    )
    return SimpleNamespace(choices=[choice], usage=usage)


@pytest.fixture
def adapter_with_text() -> tuple[OpenAILLMAdapter, AsyncMock]:
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_fake_response("Hi! Book or cancel today?")
    )
    return OpenAILLMAdapter(client=fake_client, model="gpt-4o-mini"), fake_client


@pytest.fixture
def adapter_with_tool_call() -> tuple[OpenAILLMAdapter, AsyncMock]:
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_fake_response("", [("find_patient_by_phone", {"phone": "2025550100"})])
    )
    return OpenAILLMAdapter(client=fake_client, model="gpt-4o-mini"), fake_client


async def test_adapter_returns_text_reply(
    adapter_with_text: tuple[OpenAILLMAdapter, AsyncMock],
) -> None:
    adapter, _ = adapter_with_text
    reply = await adapter.generate(
        state="GREETING", history=[{"role": "user", "content": "hi"}], tools=[]
    )
    assert isinstance(reply, LLMReply)
    assert reply.text == "Hi! Book or cancel today?"
    assert reply.tool_calls == []


async def test_adapter_parses_tool_calls(
    adapter_with_tool_call: tuple[OpenAILLMAdapter, AsyncMock],
) -> None:
    adapter, _ = adapter_with_tool_call
    reply = await adapter.generate(
        state="IDENTIFY_PATIENT",
        history=[{"role": "user", "content": "2025550100"}],
        tools=[{"type": "function", "function": {"name": "find_patient_by_phone"}}],
    )
    assert len(reply.tool_calls) == 1
    assert reply.tool_calls[0] == ToolCall(
        name="find_patient_by_phone", arguments={"phone": "2025550100"}
    )


async def test_adapter_surfaces_cached_prompt_tokens() -> None:
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_fake_response("hi", cached_tokens=1280, prompt_tokens=1700)
    )
    adapter = OpenAILLMAdapter(client=fake_client, model="gpt-4o-mini")
    reply = await adapter.generate(state="GREETING", history=[], tools=[])
    assert reply.usage is not None
    assert reply.usage.cached_prompt_tokens == 1280
    assert reply.usage.prompt_tokens == 1700
