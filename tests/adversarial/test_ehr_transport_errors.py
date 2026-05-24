"""Adversarial: EHR transport failures crash the turn instead of degrading.

Finding F-008. `EHRClient._request` only raises `EHRHTTPError` for responses
that actually arrive with a status >= 400. Transport-level failures — the EHR
process being down (ConnectError), DNS failure, or a slow EHR (ReadTimeout) —
raise raw `httpx` exceptions. The tool handlers in `tools.py` catch only
`EHRHTTPError`, and neither `Dispatcher._execute_tool` nor `_llm_turn` wraps
the handler call, so the exception propagates out of `handle_user_turn` and
crashes the whole turn. This defeats the `Result[Ok, Err]` graceful-
degradation design for the single most common real-world failure mode.
"""

from __future__ import annotations

import httpx

from prosper.ehr_client import EHRClient
from prosper.result import is_err, is_ok
from prosper.tools import find_patient_by_phone_handler


def _client_raising(exc: Exception) -> EHRClient:
    """An EHRClient whose every request raises `exc` at transport level."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise exc

    return EHRClient(transport=httpx.MockTransport(handler), base_url="http://ehr")


def _client_returning(status: int, body: dict) -> EHRClient:
    """An EHRClient whose every request returns a fixed HTTP response."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return EHRClient(transport=httpx.MockTransport(handler), base_url="http://ehr")


async def test_connect_error_degrades_to_err() -> None:
    client = _client_raising(httpx.ConnectError("connection refused"))
    async with client:
        result = await find_patient_by_phone_handler(client, phone="5551234567")
    assert is_err(result)
    assert result.code == "ehr_error"


async def test_read_timeout_degrades_to_err() -> None:
    client = _client_raising(httpx.ReadTimeout("timed out"))
    async with client:
        result = await find_patient_by_phone_handler(client, phone="5551234567")
    assert is_err(result)
    assert result.code == "ehr_error"


async def test_http_500_already_degrades_gracefully() -> None:
    """Contrast: an HTTP 500 that *arrives* IS handled — proves the gap is
    specifically transport-level, not all errors."""
    client = _client_returning(500, {"detail": "boom"})
    async with client:
        result = await find_patient_by_phone_handler(client, phone="5551234567")
    assert is_err(result)
    assert result.code == "ehr_error"


async def test_happy_path_still_ok() -> None:
    """Sanity: a normal 200 response returns Ok — guards against the fixture
    masking a real regression."""
    client = _client_returning(200, {"patients": []})
    async with client:
        result = await find_patient_by_phone_handler(client, phone="5551234567")
    assert is_ok(result)
    assert result.value["found"] is False
