"""Async httpx wrapper around the EHR HTTP API.

Two construction modes:
- ``EHRClient.for_http(base_url=...)`` for the running uvicorn process.
- ``EHRClient.for_asgi_app(app)`` mounts the FastAPI app in-process via
  ``httpx.ASGITransport`` — used by tests and the eval runner for hermetic,
  fast scenario runs (no separate process, no socket bind).

All methods return JSON-decoded dicts/lists. Errors raise ``EHRHTTPError``;
the dispatcher translates these into ``Result[Ok, Err]`` values.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional, Self

import httpx
from fastapi import FastAPI


class EHRHTTPError(Exception):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class EHRClient:
    def __init__(
        self,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        base_url: str = "http://ehr",
    ) -> None:
        self._transport = transport
        self._base_url = base_url
        self._client: Optional[httpx.AsyncClient] = None

    @classmethod
    def for_asgi_app(cls, app: FastAPI) -> Self:
        return cls(transport=httpx.ASGITransport(app=app), base_url="http://ehr")

    @classmethod
    def for_http(cls, base_url: str) -> Self:
        return cls(transport=None, base_url=base_url)

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            transport=self._transport, base_url=self._base_url, timeout=5.0
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _c(self) -> httpx.AsyncClient:
        assert self._client is not None, "use `async with` to bind the client"
        return self._client

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        r = await self._c().request(method, path, **kwargs)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except Exception:
                detail = r.text
            raise EHRHTTPError(r.status_code, detail)
        if r.status_code == 204:
            return None
        return r.json()

    async def create_patient(
        self,
        *,
        first_name: str,
        last_name: str,
        dob: date,
        phone: str,
        email: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/patients",
            json={
                "first_name": first_name,
                "last_name": last_name,
                "dob": dob.isoformat(),
                "phone": phone,
                "email": email,
            },
        )

    async def find_patients_by_phone(self, phone: str) -> list[dict[str, Any]]:
        body = await self._request("GET", "/patients/by-phone", params={"phone": phone})
        return body["patients"]

    async def find_patients_by_name_dob(
        self,
        name: str,
        dob: date,
        min_similarity: float = 0.85,
    ) -> list[dict[str, Any]]:
        body = await self._request(
            "GET",
            "/patients/by-name-dob",
            params={"name": name, "dob": dob.isoformat(), "min_similarity": min_similarity},
        )
        return body["patients"]

    async def get_upcoming_appointments(self, patient_id: str) -> list[dict[str, Any]]:
        body = await self._request("GET", f"/patients/{patient_id}/appointments")
        return body["appointments"]

    async def list_availability(
        self,
        *,
        date_: date,
        provider_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"date": date_.isoformat()}
        if provider_id is not None:
            params["provider_id"] = provider_id
        body = await self._request("GET", "/availability", params=params)
        return body["slots"]

    async def create_appointment(
        self,
        *,
        patient_id: str,
        slot_id: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/appointments",
            json={"patient_id": patient_id, "slot_id": slot_id, "notes": notes},
        )

    async def cancel_appointment(
        self,
        *,
        appointment_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/appointments/{appointment_id}/cancel",
            json={"reason": reason},
        )
