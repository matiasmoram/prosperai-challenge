"""OpenAI Chat Completions adapter conforming to ``LLMClientProtocol``.

Reliability features (README bonus #2):
- ``tenacity`` retry with exponential jitter on transient HTTP errors.
- Single fallback model (env ``PROSPER_BOT_FALLBACK_MODEL``) attempted once
  after the primary model exhausts retries — so a regional gpt-4o-mini
  brownout still completes the call against gpt-4o.

Bot remains usable when OpenAI returns 5xx briefly. Documented in
SOLUTION.md under "Reliability".
"""

from __future__ import annotations

import json
import os
from typing import Any

from loguru import logger
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from prosper.dispatcher import LLMReply, LLMUsage, ToolCall

# Transient error classes we want to retry. Permission/auth errors are NOT
# retried — they don't get better with backoff.
_RETRYABLE = (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)


class OpenAILLMAdapter:
    def __init__(
        self,
        *,
        # ``client`` is ``AsyncOpenAI`` in prod and MagicMock/AsyncMock in
        # unit tests; duck-typed on ``.chat.completions.create(**kwargs)``.
        # Kept as ``Any`` so test doubles don't need to subclass AsyncOpenAI.
        client: Any,
        model: str = "gpt-4o-mini",
        temperature: float = 0.4,
        fallback_model: str | None = None,
        max_attempts: int = 3,
        retry_wait_initial: float = 0.5,
        retry_wait_max: float = 4.0,
    ) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature
        self._fallback_model = (
            fallback_model or os.environ.get("PROSPER_BOT_FALLBACK_MODEL") or None
        )
        self._max_attempts = max_attempts
        self._retry_wait_initial = retry_wait_initial
        self._retry_wait_max = retry_wait_max

    async def generate(
        self,
        *,
        state: str,  # noqa: ARG002 — part of LLMClientProtocol; mock adapters in tests inspect it
        history: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMReply:
        try:
            return await self._call_with_retry(self._model, history, tools)
        except _RETRYABLE as primary_exc:
            if not self._fallback_model:
                raise
            logger.warning(
                "Primary model {} failed after retries ({}); trying fallback {}",
                self._model,
                type(primary_exc).__name__,
                self._fallback_model,
            )
            # One last shot on the fallback model.
            return await self._call_with_retry(self._fallback_model, history, tools)

    async def _call_with_retry(
        self, model: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMReply:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential_jitter(
                initial=self._retry_wait_initial, max=self._retry_wait_max
            ),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
        ):
            with attempt:
                return await self._single_call(model, history, tools)
        raise RuntimeError("unreachable — AsyncRetrying always returns or raises")

    async def _single_call(
        self, model: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMReply:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": history,
            "temperature": self._temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        # APIError children outside `_RETRYABLE` (auth, bad-request, etc.) will
        # propagate naturally — tenacity's `retry_if_exception_type` filters
        # for the retryable subset only.
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
            tool_calls.append(
                ToolCall(
                    name=tc.function.name,
                    arguments=args,
                    id=getattr(tc, "id", None) or "call_stub",
                )
            )
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
