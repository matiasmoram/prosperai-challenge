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
from typing import Any, Self, cast

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
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = "http://ehr",
    ) -> None:
        self._transport = transport
        self._base_url = base_url
        self._client: httpx.AsyncClient | None = None
        # Observability: per-call session UUID + per-turn integer, plus a
        # monotonically increasing call counter scoped to (session, turn).
        # Combined into an "X-Request-Id: <session>-<turn>-<n>" header on
        # every outbound request so the EHR access log lines up with the
        # bot's per-span JSON logs.
        self._session_id: str | None = None
        self._turn_id: int = 0
        self._call_seq: int = 0

    def set_session_id(self, session_id: str) -> None:
        # Concurrency note: ``_session_id`` / ``_turn_id`` / ``_call_seq`` are
        # request-scoped mutable state. The current architecture is one
        # ``EHRClient`` per ``Dispatcher`` per WebRTC call (see
        # ``bot._build_dispatcher``), and Pipecat serialises frames through
        # ``DispatcherProcessor`` sequentially (single ``__input_queue``), so
        # there is no concurrent access today. If a future change ever shares
        # one client across sessions (e.g. a pool of dispatchers), the
        # request-id sequence would race and X-Request-Id headers would
        # collide. Move the state into a ``contextvars.ContextVar`` or pass
        # it through ``_request`` kwargs before that refactor lands.
        self._session_id = session_id

    def set_turn_id(self, turn_id: int) -> None:
        # Reset the per-call sequence so the request-id stays human-readable:
        # <session>-<turn>-1, -2, -3 within a single user turn.
        self._turn_id = turn_id
        self._call_seq = 0

    def _next_request_id(self) -> str | None:
        if self._session_id is None:
            return None
        self._call_seq += 1
        return f"{self._session_id}-{self._turn_id}-{self._call_seq}"

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
        req_id = self._next_request_id()
        if req_id is not None:
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("X-Request-Id", req_id)
            kwargs["headers"] = headers
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
        return cast(
            "dict[str, Any]",
            await self._request(
                "POST",
                "/patients",
                json={
                    "first_name": first_name,
                    "last_name": last_name,
                    "dob": dob.isoformat(),
                    "phone": phone,
                    "email": email,
                },
            ),
        )

    async def find_patients_by_phone(self, phone: str) -> list[dict[str, Any]]:
        body = await self._request("GET", "/patients/by-phone", params={"phone": phone})
        return cast("list[dict[str, Any]]", body["patients"])

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
        return cast("list[dict[str, Any]]", body["patients"])

    async def get_upcoming_appointments(self, patient_id: str) -> list[dict[str, Any]]:
        body = await self._request("GET", f"/patients/{patient_id}/appointments")
        return cast("list[dict[str, Any]]", body["appointments"])

    async def list_availability(
        self,
        *,
        date_: date,
        provider_id: str | None = None,
        specialty: str | None = None,
        duration_minutes: int = 30,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"date": date_.isoformat(), "duration_minutes": duration_minutes}
        if provider_id is not None:
            params["provider_id"] = provider_id
        if specialty is not None:
            params["specialty"] = specialty
        body = await self._request("GET", "/availability", params=params)
        return cast("list[dict[str, Any]]", body["slots"])

    async def create_appointment(
        self,
        *,
        patient_id: str,
        slot_id: str,
        duration_minutes: int = 30,
        notes: str | None = None,
    ) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._request(
                "POST",
                "/appointments",
                json={
                    "patient_id": patient_id,
                    "slot_id": slot_id,
                    "duration_minutes": duration_minutes,
                    "notes": notes,
                },
            ),
        )

    async def cancel_appointment(
        self,
        *,
        appointment_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._request(
                "POST",
                f"/appointments/{appointment_id}/cancel",
                json={"reason": reason},
            ),
        )

    async def reschedule_appointment(
        self,
        *,
        appointment_id: str,
        new_slot_id: str,
        new_duration_minutes: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"new_slot_id": new_slot_id}
        if new_duration_minutes is not None:
            body["new_duration_minutes"] = new_duration_minutes
        return cast(
            "dict[str, Any]",
            await self._request(
                "PATCH",
                f"/appointments/{appointment_id}",
                json=body,
            ),
        )
