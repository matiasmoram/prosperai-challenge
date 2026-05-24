"""Dispatcher-level wiring for symptom triage (ADR 005).

Covers the three integration points the unit tests in ``test_triage.py`` don't:
the LLM-facing redaction of a ``suggest_specialty`` result, the PII masking of
its ``symptoms`` argument in the operator-console audit payload, and the
recording of the recommendation into ``SessionMemory`` after a real turn.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

import prosper.llm as prosper_llm
from prosper.dispatcher import (
    Dispatcher,
    LLMClientProtocol,
    LLMReply,
    ToolCall,
    _redact_for_llm,
    _redact_tool_args,
)
from prosper.ehr_client import EHRClient
from prosper.flows import State

# --- pure redaction functions -------------------------------------------------


def test_redact_for_llm_triage_line_without_follow_up() -> None:
    line = _redact_for_llm(
        "suggest_specialty",
        {
            "specialty": "Psychiatrist",
            "duration_minutes": 60,
            "minimum_safe_minutes": 30,
            "confidence": 0.91,
            "rationale": "Sixty minutes recommended for psychiatric assessment.",
        },
    )
    assert "Psychiatrist" in line
    assert "duration_minutes=60 (recommended)" in line
    assert "minimum_safe_minutes=30 (clinical floor)" in line
    assert "0.91" in line
    assert "rationale:" in line
    assert "ask the caller" not in line


def test_redact_for_llm_triage_line_with_follow_up() -> None:
    line = _redact_for_llm(
        "suggest_specialty",
        {
            "specialty": "General Practice",
            "duration_minutes": 30,
            "minimum_safe_minutes": 30,
            "confidence": 0.4,
            "rationale": "",
            "follow_up": "Is it physical or emotional?",
        },
    )
    assert "ask the caller" in line
    assert "Is it physical or emotional?" in line


def test_redact_tool_args_masks_symptoms() -> None:
    masked = _redact_tool_args("suggest_specialty", {"symptoms": "chest pain two days"})
    assert masked == {"symptoms": "[SYMPTOMS]"}
    # Empty / missing symptoms collapses to a sentinel, never the raw text.
    assert _redact_tool_args("suggest_specialty", {"symptoms": ""}) == {"symptoms": "(none)"}


# --- full turn records the recommendation into memory -------------------------


class _CannedLLM(LLMClientProtocol):
    def __init__(self, replies: list[LLMReply]) -> None:
        self._iter: Iterator[LLMReply] = iter(replies)

    async def generate(self, *, state: str, history: list[dict], tools: list[dict]) -> LLMReply:
        try:
            return next(self._iter)
        except StopIteration:
            return LLMReply(text="", tool_calls=[])


class _FixedTriageResponse:
    def __init__(self, content: str) -> None:
        msg = type("Msg", (), {"content": content})()
        choice = type("Choice", (), {"message": msg})()
        self.choices = [choice]


class _FixedTriageCompletions:
    async def create(self, **_: Any) -> _FixedTriageResponse:
        return _FixedTriageResponse(
            json.dumps(
                {
                    "specialty": "Psychiatrist",
                    "duration_minutes": 60,
                    "minimum_safe_minutes": 30,
                    "confidence": 0.9,
                    "follow_up": None,
                    "red_flag": False,
                    "rationale": (
                        "Sixty minutes recommended for psychiatric assessment; "
                        "thirty is the clinical minimum."
                    ),
                }
            )
        )


class _FakeTriageClient:
    """AsyncOpenAI stand-in returning a fixed classification regardless of input."""

    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": _FixedTriageCompletions()})()


@pytest.fixture
def _triage_override() -> Iterator[None]:
    prev = prosper_llm._TRIAGE_CLIENT_OVERRIDE
    prosper_llm._TRIAGE_CLIENT_OVERRIDE = _FakeTriageClient()
    try:
        yield
    finally:
        prosper_llm._TRIAGE_CLIENT_OVERRIDE = prev


async def test_suggest_specialty_records_recommendation_in_memory(
    ehr_client: EHRClient, _triage_override: None
) -> None:
    canned = _CannedLLM(
        [
            LLMReply(
                text="",
                tool_calls=[ToolCall(name="suggest_specialty", arguments={"symptoms": "down"})],
            ),
            LLMReply(text="Let's get you in with a psychiatrist. What day?", tool_calls=[]),
        ]
    )
    async with ehr_client:
        d = Dispatcher(llm=canned, ehr_client=ehr_client)
        d.state = State.BOOK_FLOW
        d.memory.identified_patient = {"id": "p1", "first_name": "Ada", "last_name": "L"}
        await d.handle_user_turn("I've been feeling really down")
    assert d.memory.recommended_specialty == "Psychiatrist"
    assert d.memory.recommended_duration_minutes == 60
    assert d.memory.minimum_safe_minutes == 30
    # The LLM-facing tool result is the redacted triage line, not raw JSON.
    triage_tool_msgs = [
        m for m in d.history if m.get("role") == "tool" and "triage:" in str(m.get("content", ""))
    ]
    assert triage_tool_msgs, d.history
    # Both recommended and floor durations must be surfaced so the LLM can
    # negotiate per the BOOK_FLOW task message rules.
    content = triage_tool_msgs[0]["content"]
    assert "duration_minutes=60 (recommended)" in content
    assert "minimum_safe_minutes=30 (clinical floor)" in content
