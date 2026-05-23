"""Tests for symptom triage: the mini-LLM classifier (`llm.classify_symptoms`)
and the tool handler (`tools.suggest_specialty_handler`).

The mini-LLM is faked with an object exposing
``chat.completions.create`` so no network / API key is needed. Every Err
code is pinned because the dispatcher and eval scenarios branch on it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from prosper.llm import SpecialtyClassification, classify_symptoms
from prosper.result import is_err, is_ok
from prosper.tools import suggest_specialty_handler


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, *, content: str | None = None, raises: Exception | None = None) -> None:
        self._content = content
        self._raises = raises
        self.last_kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self.last_kwargs = kwargs
        if self._raises is not None:
            raise self._raises
        assert self._content is not None
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeOpenAI:
    """Minimal stand-in for AsyncOpenAI with the .chat.completions.create path."""

    def __init__(self, *, content: str | None = None, raises: Exception | None = None) -> None:
        self.chat = _FakeChat(_FakeCompletions(content=content, raises=raises))


def _classification_json(**overrides: Any) -> str:
    base = {
        "specialty": "General Practice",
        "duration_minutes": 30,
        "confidence": 0.9,
        "follow_up": None,
        "red_flag": False,
    }
    base.update(overrides)
    return json.dumps(base)


# ---------------------------------------------------------------------------
# classify_symptoms — happy path + every Err branch
# ---------------------------------------------------------------------------


async def test_classify_symptoms_happy_path() -> None:
    client = _FakeOpenAI(
        content=_classification_json(specialty="Psychiatrist", duration_minutes=60, confidence=0.92)
    )
    r = await classify_symptoms(symptoms="I've been feeling very anxious", client=client)
    assert is_ok(r)
    cls = r.value
    assert isinstance(cls, SpecialtyClassification)
    assert cls.specialty == "Psychiatrist"
    assert cls.duration_minutes == 60
    assert cls.confidence == pytest.approx(0.92)
    assert cls.follow_up is None
    assert cls.red_flag is False


async def test_classify_symptoms_uses_json_schema_response_format() -> None:
    client = _FakeOpenAI(content=_classification_json())
    await classify_symptoms(symptoms="stomach ache", client=client)
    kwargs = client.chat.completions.last_kwargs
    assert kwargs is not None
    assert kwargs["response_format"]["type"] == "json_schema"
    assert kwargs["temperature"] == 0.0


async def test_classify_symptoms_low_confidence_carries_follow_up() -> None:
    client = _FakeOpenAI(
        content=_classification_json(confidence=0.4, follow_up="Is it more physical or emotional?")
    )
    r = await classify_symptoms(symptoms="I just feel off", client=client)
    assert is_ok(r)
    assert r.value.confidence == pytest.approx(0.4)
    assert r.value.follow_up == "Is it more physical or emotional?"


async def test_classify_symptoms_empty_input_is_err() -> None:
    r = await classify_symptoms(symptoms="   ", client=_FakeOpenAI(content="{}"))
    assert is_err(r)
    assert r.code == "triage_unavailable"


async def test_classify_symptoms_malformed_json_is_err() -> None:
    r = await classify_symptoms(symptoms="headache", client=_FakeOpenAI(content="not json{"))
    assert is_err(r)
    assert r.code == "triage_unavailable"


async def test_classify_symptoms_unknown_specialty_is_err() -> None:
    client = _FakeOpenAI(content=_classification_json(specialty="Cardiologist"))
    r = await classify_symptoms(symptoms="chest tightness", client=client)
    assert is_err(r)
    assert r.code == "unknown_specialty"


async def test_classify_symptoms_invalid_duration_is_err() -> None:
    client = _FakeOpenAI(content=_classification_json(duration_minutes=45))
    r = await classify_symptoms(symptoms="back pain", client=client)
    assert is_err(r)
    assert r.code == "invalid_duration"


async def test_classify_symptoms_transport_error_is_err() -> None:
    from openai import APITimeoutError

    client = _FakeOpenAI(raises=APITimeoutError(request=None))  # type: ignore[arg-type]
    r = await classify_symptoms(symptoms="dizzy", client=client)
    assert is_err(r)
    assert r.code == "triage_unavailable"
    assert r.retryable is True


# ---------------------------------------------------------------------------
# suggest_specialty_handler — wraps classify_symptoms, adds red-flag escalation
# ---------------------------------------------------------------------------


async def test_suggest_specialty_handler_ok_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_classify(*, symptoms: str, **_: Any) -> Any:
        from prosper.result import Ok

        return Ok(
            value=SpecialtyClassification(
                specialty="Therapist", duration_minutes=60, confidence=0.88
            )
        )

    monkeypatch.setattr("prosper.llm.classify_symptoms", _fake_classify)
    r = await suggest_specialty_handler(None, symptoms="I'm struggling with grief")  # type: ignore[arg-type]
    assert is_ok(r)
    assert r.value["specialty"] == "Therapist"
    assert r.value["duration_minutes"] == 60
    assert r.value["confidence"] == pytest.approx(0.88)
    assert r.value["follow_up"] is None


async def test_suggest_specialty_handler_red_flag_escalates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_classify(*, symptoms: str, **_: Any) -> Any:
        from prosper.result import Ok

        return Ok(
            value=SpecialtyClassification(
                specialty="General Practice",
                duration_minutes=30,
                confidence=0.95,
                red_flag=True,
            )
        )

    monkeypatch.setattr("prosper.llm.classify_symptoms", _fake_classify)
    r = await suggest_specialty_handler(None, symptoms="crushing chest pain")  # type: ignore[arg-type]
    assert is_err(r)
    assert r.code == "medical_emergency"
    assert r.retryable is False


async def test_suggest_specialty_handler_propagates_classifier_err(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_classify(*, symptoms: str, **_: Any) -> Any:
        from prosper.result import Err

        return Err(code="triage_unavailable", message="boom", retryable=True)

    monkeypatch.setattr("prosper.llm.classify_symptoms", _fake_classify)
    r = await suggest_specialty_handler(None, symptoms="whatever")  # type: ignore[arg-type]
    assert is_err(r)
    assert r.code == "triage_unavailable"
