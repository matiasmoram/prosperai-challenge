"""OpenAI Chat Completions adapter conforming to ``LLMClientProtocol``."""

from __future__ import annotations

import json
from typing import Any

from prosper.dispatcher import LLMReply, LLMUsage, ToolCall


class OpenAILLMAdapter:
    def __init__(
        self,
        *,
        client: Any,
        model: str = "gpt-4o-mini",
        temperature: float = 0.4,
    ) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature

    async def generate(self, *, state: str, history: list[dict], tools: list[dict]) -> LLMReply:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": history,
            "temperature": self._temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = await self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message
        text = msg.content or ""
        tool_calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(name=tc.function.name, arguments=args))
        # OpenAI returns cached_tokens nested under prompt_tokens_details; guard
        # for absence so other providers / mocked tests still work.
        usage = getattr(resp, "usage", None)
        cached = 0
        prompt = 0
        completion = 0
        if usage is not None:
            prompt = getattr(usage, "prompt_tokens", 0) or 0
            completion = getattr(usage, "completion_tokens", 0) or 0
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                cached = getattr(details, "cached_tokens", 0) or 0
        return LLMReply(
            text=text,
            tool_calls=tool_calls,
            usage=LLMUsage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                cached_prompt_tokens=cached,
            ),
        )
