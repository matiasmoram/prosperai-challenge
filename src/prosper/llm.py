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
from dataclasses import dataclass
from typing import Any

from loguru import logger
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
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
from prosper.prompts import SPECIALTY_DURATION_TABLE, TRIAGE_SYSTEM_PROMPT
from prosper.result import Err, Ok, Result

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


# ──────────────────────────────────────────────────────────────────────
# Mini-LLM symptom triage (single JSON-mode call, no tools, no retries).
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SpecialtyClassification:
    """Mini-LLM triage result. Used by ``tools.suggest_specialty_handler``."""

    specialty: str
    duration_minutes: int
    confidence: float
    follow_up: str | None = None
    red_flag: bool = False


# Strict JSON schema we ask gpt-4o-mini to emit. `additionalProperties:
# false` keeps the model honest — it can't slip extra prose into a field.
_TRIAGE_JSON_SCHEMA: dict[str, Any] = {
    "name": "specialty_classification",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "specialty",
            "duration_minutes",
            "confidence",
            "follow_up",
            "red_flag",
        ],
        "properties": {
            "specialty": {
                "type": "string",
                "enum": list(SPECIALTY_DURATION_TABLE.keys()),
            },
            "duration_minutes": {"type": "integer", "enum": [30, 60, 90]},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "follow_up": {"type": ["string", "null"]},
            "red_flag": {"type": "boolean"},
        },
    },
}


# Process-lifetime shared client for the triage mini-LLM call. Lazy so
# imports don't blow up when OPENAI_API_KEY is unset (tests / mock-eval).
# Tests can override by assigning ``_TRIAGE_CLIENT_OVERRIDE``.
_TRIAGE_CLIENT_OVERRIDE: Any = None
_TRIAGE_CLIENT: Any = None


def _get_triage_client() -> Any:
    """Return the shared AsyncOpenAI client for triage, creating it on first use.

    Tests inject a fake by setting ``_TRIAGE_CLIENT_OVERRIDE`` to any
    object with the ``.chat.completions.create(**kwargs)`` interface.
    """
    if _TRIAGE_CLIENT_OVERRIDE is not None:
        return _TRIAGE_CLIENT_OVERRIDE
    global _TRIAGE_CLIENT
    if _TRIAGE_CLIENT is None:
        _TRIAGE_CLIENT = AsyncOpenAI()
    return _TRIAGE_CLIENT


async def classify_symptoms(
    *,
    symptoms: str,
    model: str = "gpt-4o-mini",
    client: Any | None = None,
) -> Result[SpecialtyClassification]:
    """Single mini-LLM call that maps a symptom description to (specialty, duration).

    Returns ``Ok(SpecialtyClassification)`` on success, or ``Err`` with
    one of: ``triage_unavailable`` (transport / 5xx / JSON malformed),
    ``unknown_specialty`` (model returned something outside the allowed
    enum — should be impossible with strict mode, kept as belt-and-braces),
    ``invalid_duration`` (same idea for duration). Caller layers
    confidence / red_flag handling on top.

    No tenacity retry: this fires inside a tool handler that the main
    LLM already retries via the outer dispatcher loop. A retry here
    would just double the latency tail on a real outage.
    """
    if not symptoms or not symptoms.strip():
        return Err(
            code="triage_unavailable",
            message="empty symptom description",
            retryable=False,
        )
    triage_client = client if client is not None else _get_triage_client()
    allowed_text = ", ".join(
        f"{spec} (default {mins} min)" for spec, mins in SPECIALTY_DURATION_TABLE.items()
    )
    system_msg = f"{TRIAGE_SYSTEM_PROMPT}\n\nAllowed specialties: {allowed_text}."
    try:
        resp = await triage_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": symptoms.strip()},
            ],
            temperature=0.0,
            response_format={"type": "json_schema", "json_schema": _TRIAGE_JSON_SCHEMA},
        )
    except _RETRYABLE as e:
        return Err(
            code="triage_unavailable",
            message=f"mini-LLM transient failure: {type(e).__name__}",
            retryable=True,
        )
    except Exception as e:
        # Any non-retryable client error is fatal for this single call —
        # we fail closed so the agent asks the caller to name a specialty.
        return Err(
            code="triage_unavailable",
            message=f"mini-LLM call failed: {type(e).__name__}: {e}",
            retryable=False,
        )
    content = resp.choices[0].message.content or "{}"
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return Err(
            code="triage_unavailable",
            message=f"mini-LLM returned non-JSON: {content[:120]}",
            retryable=False,
        )
    specialty = parsed.get("specialty", "")
    if specialty not in SPECIALTY_DURATION_TABLE:
        return Err(
            code="unknown_specialty",
            message=f"mini-LLM returned specialty {specialty!r} not in allowed set",
            retryable=False,
        )
    duration = parsed.get("duration_minutes")
    if duration not in (30, 60, 90):
        return Err(
            code="invalid_duration",
            message=f"mini-LLM returned duration_minutes={duration!r} outside {{30,60,90}}",
            retryable=False,
        )
    follow_up_raw = parsed.get("follow_up")
    return Ok(
        value=SpecialtyClassification(
            specialty=specialty,
            duration_minutes=int(duration),
            confidence=float(parsed.get("confidence", 0.0)),
            follow_up=follow_up_raw if isinstance(follow_up_raw, str) and follow_up_raw else None,
            red_flag=bool(parsed.get("red_flag", False)),
        )
    )
