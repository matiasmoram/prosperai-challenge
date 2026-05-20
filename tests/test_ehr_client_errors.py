"""Coverage-gap tests for ``prosper.ehr_client`` HTTP error / edge paths."""

from __future__ import annotations

import httpx
import pytest

from prosper.ehr_client import EHRClient, EHRHTTPError


def _client_with_mock_transport(handler) -> EHRClient:  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)
    return EHRClient(transport=transport, base_url="http://ehr-test")


async def test_request_raises_ehr_http_error_with_json_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": {"code": "slot_taken"}})

    client = _client_with_mock_transport(handler)
    async with client:
        with pytest.raises(EHRHTTPError) as excinfo:
            await client.find_patients_by_phone("2025550100")
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == {"code": "slot_taken"}


async def test_request_falls_back_to_text_detail_when_json_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"upstream tipped over", headers={})

    client = _client_with_mock_transport(handler)
    async with client:
        with pytest.raises(EHRHTTPError) as excinfo:
            await client.find_patients_by_phone("2025550100")
    assert excinfo.value.status_code == 502
    assert "upstream tipped over" in str(excinfo.value.detail)


async def test_request_returns_none_on_204() -> None:
    """Cover the 204 No-Content branch in EHRClient._request."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    client = _client_with_mock_transport(handler)
    async with client:
        # Call _request directly — none of the typed methods returns None today.
        result = await client._request("DELETE", "/x")
    assert result is None


def test_for_http_constructor_sets_base_url_and_no_transport() -> None:
    """Cover EHRClient.for_http (only for_asgi_app is exercised elsewhere)."""
    c = EHRClient.for_http("https://ehr.example.com")
    assert c._base_url == "https://ehr.example.com"
    assert c._transport is None


async def test_client_assertion_fires_without_async_with() -> None:
    """_c() must raise if the user forgot the async-with block."""
    c = EHRClient.for_http("http://ehr-test")
    with pytest.raises(AssertionError):
        c._c()


async def test_request_id_header_is_session_turn_seq() -> None:
    """Every outbound EHR call carries X-Request-Id = <session>-<turn>-<n>.
    Contract: the seq counter resets on each new turn so ids stay readable,
    and the header is omitted entirely when no session id has been set
    (e.g. tests that build an EHRClient directly without a Dispatcher)."""
    captured: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("x-request-id"))
        return httpx.Response(200, json={"patients": []})

    client = _client_with_mock_transport(handler)
    client.set_session_id("sess-XYZ")
    client.set_turn_id(3)
    async with client:
        await client.find_patients_by_phone("2025550100")
        await client.find_patients_by_phone("2025550100")
        client.set_turn_id(4)
        await client.find_patients_by_phone("2025550100")
    assert captured == ["sess-XYZ-3-1", "sess-XYZ-3-2", "sess-XYZ-4-1"]


async def test_request_id_header_absent_when_session_not_set() -> None:
    captured: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("x-request-id"))
        return httpx.Response(200, json={"patients": []})

    client = _client_with_mock_transport(handler)
    async with client:
        await client.find_patients_by_phone("2025550100")
    assert captured == [None]
