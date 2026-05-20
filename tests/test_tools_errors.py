"""Coverage-gap tests for ``prosper.tools`` Err code branches.

Every Err code emitted by a tool handler is something the dispatcher and the
eval scenarios assert against. These tests pin the codes so a refactor can't
silently rename one.
"""

from __future__ import annotations

from typing import Any

import pytest

from prosper.ehr_client import EHRClient, EHRHTTPError
from prosper.result import is_err
from prosper.tools import (
    _parse_dob,
    cancel_appointment_handler,
    create_appointment_handler,
    create_patient_handler,
    find_patient_by_name_dob_handler,
    find_patient_by_phone_handler,
    get_upcoming_appointments_handler,
    list_availability_slots_handler,
)

# ---------------------------------------------------------------------------
# DOB / date unparseable -> typed Err
# ---------------------------------------------------------------------------


def test_parse_dob_returns_err_for_garbage() -> None:
    r = _parse_dob("not a real date 🍕")
    assert is_err(r)
    assert r.code == "dob_unparseable"
    assert r.retryable is True


async def test_find_patient_by_name_dob_handler_propagates_dob_err() -> None:
    # Client is never reached because DOB parsing fails first.
    r = await find_patient_by_name_dob_handler(
        _UnreachableClient(), name="Ada", dob="absolutely-not-a-date"
    )
    assert is_err(r)
    assert r.code == "dob_unparseable"


async def test_create_patient_handler_propagates_dob_err() -> None:
    r = await create_patient_handler(
        _UnreachableClient(),
        first_name="Ada",
        last_name="L",
        dob="never",
        phone="2025550100",
    )
    assert is_err(r)
    assert r.code == "dob_unparseable"


async def test_list_availability_slots_handler_returns_date_unparseable() -> None:
    r = await list_availability_slots_handler(_UnreachableClient(), date="never")
    assert is_err(r)
    assert r.code == "date_unparseable"
    assert r.retryable is True


# ---------------------------------------------------------------------------
# EHRHTTPError -> typed Err for every handler that calls the EHR
# ---------------------------------------------------------------------------


class _RaisingClient:
    """Async EHR-client double that raises EHRHTTPError on every call."""

    def __init__(self, *, status_code: int = 500, detail: Any = "boom") -> None:
        self._status = status_code
        self._detail = detail

    def _raise(self) -> None:
        raise EHRHTTPError(self._status, self._detail)

    async def find_patients_by_phone(self, _phone: str) -> list[dict[str, Any]]:
        self._raise()

    async def find_patients_by_name_dob(
        self, _name: str, _dob: Any, min_similarity: float = 0.85
    ) -> list[dict[str, Any]]:
        self._raise()

    async def create_patient(self, **_kw: Any) -> dict[str, Any]:
        self._raise()

    async def list_availability(self, **_kw: Any) -> list[dict[str, Any]]:
        self._raise()

    async def create_appointment(self, **_kw: Any) -> dict[str, Any]:
        self._raise()

    async def get_upcoming_appointments(self, _pid: str) -> list[dict[str, Any]]:
        self._raise()

    async def cancel_appointment(self, **_kw: Any) -> dict[str, Any]:
        self._raise()


class _UnreachableClient(_RaisingClient):
    """Test client whose methods raise AssertionError if any are called.

    Used to assert that argument-parsing errors short-circuit the network call.
    """

    def _raise(self) -> None:
        raise AssertionError("client should not have been called")


async def test_find_patient_by_phone_returns_ehr_error_on_http_error() -> None:
    r = await find_patient_by_phone_handler(_RaisingClient(), phone="2025550100")  # type: ignore[arg-type]
    assert is_err(r)
    assert r.code == "ehr_error"
    assert r.retryable is True


async def test_find_patient_by_name_dob_returns_ehr_error_on_http_error() -> None:
    r = await find_patient_by_name_dob_handler(  # type: ignore[arg-type]
        _RaisingClient(), name="Ada", dob="1990-12-10"
    )
    assert is_err(r)
    assert r.code == "ehr_error"


async def test_create_patient_returns_patient_exists_on_409() -> None:
    r = await create_patient_handler(  # type: ignore[arg-type]
        _RaisingClient(status_code=409, detail={"code": "patient_exists"}),
        first_name="Ada",
        last_name="L",
        dob="1990-12-10",
        phone="2025550100",
    )
    assert is_err(r)
    assert r.code == "patient_exists"
    assert r.retryable is False


async def test_create_patient_returns_ehr_error_on_other_status() -> None:
    r = await create_patient_handler(  # type: ignore[arg-type]
        _RaisingClient(status_code=500, detail="db down"),
        first_name="Ada",
        last_name="L",
        dob="1990-12-10",
        phone="2025550100",
    )
    assert is_err(r)
    assert r.code == "ehr_error"


async def test_list_availability_returns_ehr_error_on_http_error() -> None:
    r = await list_availability_slots_handler(  # type: ignore[arg-type]
        _RaisingClient(), date="2026-05-21"
    )
    assert is_err(r)
    assert r.code == "ehr_error"


async def test_create_appointment_returns_slot_taken_other_patient_on_409() -> None:
    r = await create_appointment_handler(  # type: ignore[arg-type]
        _RaisingClient(status_code=409, detail={"code": "slot_taken"}),
        patient_id="pid",
        slot_id="sid",
    )
    assert is_err(r)
    assert r.code == "slot_taken_other_patient"
    assert r.retryable is True


async def test_create_appointment_returns_patient_or_slot_not_found_on_404() -> None:
    r = await create_appointment_handler(  # type: ignore[arg-type]
        _RaisingClient(status_code=404, detail={"code": "patient_not_found"}),
        patient_id="pid",
        slot_id="sid",
    )
    assert is_err(r)
    assert r.code == "patient_or_slot_not_found"
    assert r.retryable is False


async def test_create_appointment_returns_ehr_error_on_500() -> None:
    r = await create_appointment_handler(  # type: ignore[arg-type]
        _RaisingClient(status_code=500, detail="db down"),
        patient_id="pid",
        slot_id="sid",
    )
    assert is_err(r)
    assert r.code == "ehr_error"


async def test_create_appointment_returns_ehr_error_on_409_with_other_detail() -> None:
    """409 that isn't slot_taken falls through to generic ehr_error."""
    r = await create_appointment_handler(  # type: ignore[arg-type]
        _RaisingClient(status_code=409, detail="something else"),
        patient_id="pid",
        slot_id="sid",
    )
    assert is_err(r)
    assert r.code == "ehr_error"


async def test_get_upcoming_appointments_returns_ehr_error_on_http_error() -> None:
    r = await get_upcoming_appointments_handler(_RaisingClient(), patient_id="pid")  # type: ignore[arg-type]
    assert is_err(r)
    assert r.code == "ehr_error"


async def test_cancel_appointment_returns_appointment_not_found_on_404() -> None:
    r = await cancel_appointment_handler(  # type: ignore[arg-type]
        _RaisingClient(status_code=404, detail={"code": "appointment_not_found"}),
        appointment_id="appt-x",
    )
    assert is_err(r)
    assert r.code == "appointment_not_found"
    assert r.retryable is False


async def test_cancel_appointment_returns_ehr_error_on_500() -> None:
    r = await cancel_appointment_handler(  # type: ignore[arg-type]
        _RaisingClient(status_code=500, detail="db down"),
        appointment_id="appt-x",
    )
    assert is_err(r)
    assert r.code == "ehr_error"


# ---------------------------------------------------------------------------
# End-to-end through the real EHR ASGI app: covers create_patient -> 409
# patient_exists branch that needs detail.get("code") parsing.
# ---------------------------------------------------------------------------


# ``asgi_client`` is provided by ``tests/conftest.py``.


async def test_create_patient_duplicate_phone_returns_patient_exists(
    asgi_client: EHRClient,
) -> None:
    """End-to-end: registering the same phone twice exercises the 409 path."""
    async with asgi_client:
        ok = await create_patient_handler(
            asgi_client,
            first_name="Ada",
            last_name="L",
            dob="1990-12-10",
            phone="2025550100",
        )
        assert ok.kind == "ok"
        dup = await create_patient_handler(
            asgi_client,
            first_name="Ada",
            last_name="L",
            dob="1990-12-10",
            phone="2025550100",
        )
    assert is_err(dup)
    assert dup.code == "patient_exists"
    assert dup.retryable is False


async def test_create_appointment_unknown_slot_returns_patient_or_slot_not_found(
    asgi_client: EHRClient,
) -> None:
    async with asgi_client:
        ok = await create_patient_handler(
            asgi_client,
            first_name="Ada",
            last_name="L",
            dob="1990-12-10",
            phone="2025550100",
        )
        pid = ok.value["patient_id"]
        r = await create_appointment_handler(
            asgi_client, patient_id=pid, slot_id="00000000-0000-0000-0000-000000000000"
        )
    assert is_err(r)
    assert r.code == "patient_or_slot_not_found"


# ---------------------------------------------------------------------------
# Bug-fuzz regression: _parse_dob must NOT silently mine a year out of text.
# Previously `fuzzy=True` returned (today's m/d + 1990) for "hello 1990" and
# similar — a data-corruption bug since the LLM forwards user phrases verbatim.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "garbage",
    [
        "hello 1990",
        "book me may",
        "April third 1992",  # 'third' is non-numeric → ambiguous → must reject
        "today",
        "yesterday",
        "year zero",
        "I was born sometime in 1985 I think",
    ],
)
def test_parse_dob_rejects_fuzzy_yearmining(garbage: str) -> None:
    r = _parse_dob(garbage)
    assert is_err(r), f"_parse_dob silently accepted {garbage!r}"
    assert r.code == "dob_unparseable"


@pytest.mark.parametrize("out_of_range", ["1700-01-01", "3000-01-01", "0050-06-01"])
def test_parse_dob_rejects_out_of_range_years(out_of_range: str) -> None:
    r = _parse_dob(out_of_range)
    assert is_err(r)
    assert r.code == "dob_unparseable"


def test_parse_dob_rejects_empty_and_whitespace() -> None:
    for raw in ("", "   ", "\t\n"):
        r = _parse_dob(raw)
        assert is_err(r), f"empty input {raw!r} should fail"


def test_parse_dob_still_accepts_well_formed_dates() -> None:
    for raw in ("1990-12-10", "April 3 1992", "Apr 3, 1992", "12/10/1990"):
        r = _parse_dob(raw)
        assert not is_err(r), f"strict parse rejected well-formed {raw!r}"


# Mutation-survivor regression: line-coverage misses inclusive vs exclusive
# range boundaries. ``[1900, 2100]`` must accept the exact endpoints.
def test_parse_dob_accepts_inclusive_year_endpoints() -> None:
    """Mutation: ``year <= _MAX_PARSED_YEAR`` → ``year < _MAX_PARSED_YEAR``."""
    for raw in ("1900-01-01", "2100-12-31"):
        r = _parse_dob(raw)
        assert not is_err(r), (
            f"_parse_dob must accept boundary year {raw!r} (range is inclusive)"
        )
