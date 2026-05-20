"""Tool handlers exposed to the LLM via OpenAI function-calling.

Each handler:
1. Coerces / validates string inputs (DOB parsing, etc).
2. Calls the EHR via the shared ``EHRClient``.
3. Returns ``Result[Ok[dict], Err]`` with a stable ``code`` enum on the Err
   side. The Err code is what the dispatcher branches on AND what eval
   scenarios assert against — so codes must NEVER drift silently.

The OpenAI tool schemas live in ``TOOL_SCHEMAS`` below; the per-state
whitelist in ``flows.py`` references them by name.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from dateutil import parser as dateparser

from prosper.ehr_client import EHRClient, EHRHTTPError
from prosper.result import Err, Ok, Result

ToolHandler = Callable[..., Awaitable[Result[dict[str, Any]]]]

# Defensive bounds for any parsed date used downstream — DOBs and availability
# query dates alike. Catches obviously-wrong values (year 9999 typos, dateutil
# fuzzy-parser inventing 1990 from a stray digit) before they hit the DB.
_MIN_PARSED_YEAR = 1900
_MAX_PARSED_YEAR = 2100


def _parse_dob(raw: str) -> Result[date]:
    # `fuzzy=True` previously made the parser silently extract a year from
    # arbitrary text ("hello 1990" → 1990-05-20). For both DOB and date-of-
    # service we want strict parsing; ambiguous input should fail loudly so the
    # LLM re-asks the user.
    if not isinstance(raw, str) or not raw.strip():
        return Err(code="dob_unparseable", message=f"empty date input: {raw!r}", retryable=True)
    try:
        parsed = dateparser.parse(raw, dayfirst=False, fuzzy=False).date()
    except (ValueError, TypeError, AttributeError, OverflowError) as e:
        return Err(code="dob_unparseable", message=f"could not parse '{raw}': {e}", retryable=True)
    if not (_MIN_PARSED_YEAR <= parsed.year <= _MAX_PARSED_YEAR):
        return Err(
            code="dob_unparseable",
            message=(
                f"year {parsed.year} out of supported range "
                f"[{_MIN_PARSED_YEAR}, {_MAX_PARSED_YEAR}]"
            ),
            retryable=True,
        )
    return Ok(value=parsed)


async def find_patient_by_phone_handler(client: EHRClient, *, phone: str) -> Result[dict[str, Any]]:
    try:
        patients = await client.find_patients_by_phone(phone)
    except EHRHTTPError as e:
        return Err(code="ehr_error", message=str(e), retryable=True)
    return Ok(value={"found": bool(patients), "patients": patients})


async def find_patient_by_name_dob_handler(
    client: EHRClient, *, name: str, dob: str
) -> Result[dict[str, Any]]:
    dob_r = _parse_dob(dob)
    if dob_r.kind == "err":
        return dob_r
    try:
        patients = await client.find_patients_by_name_dob(name, dob_r.value)
    except EHRHTTPError as e:
        return Err(code="ehr_error", message=str(e), retryable=True)
    return Ok(value={"found": bool(patients), "patients": patients})


async def create_patient_handler(
    client: EHRClient,
    *,
    first_name: str,
    last_name: str,
    dob: str,
    phone: str,
    email: str | None = None,
) -> Result[dict[str, Any]]:
    dob_r = _parse_dob(dob)
    if dob_r.kind == "err":
        return dob_r
    try:
        created = await client.create_patient(
            first_name=first_name,
            last_name=last_name,
            dob=dob_r.value,
            phone=phone,
            email=email,
        )
    except EHRHTTPError as e:
        code = "patient_exists" if e.status_code == 409 else "ehr_error"
        return Err(code=code, message=str(e), retryable=False)
    return Ok(
        value={
            "patient_id": created["id"],
            "first_name": created["first_name"],
            "last_name": created["last_name"],
            "phone": created["phone"],
        }
    )


async def list_availability_slots_handler(
    client: EHRClient,
    *,
    date: str,
    provider_id: str | None = None,
) -> Result[dict[str, Any]]:
    d_r = _parse_dob(date)
    if d_r.kind == "err":
        return Err(code="date_unparseable", message=d_r.message, retryable=True)
    try:
        slots = await client.list_availability(date_=d_r.value, provider_id=provider_id)
    except EHRHTTPError as e:
        return Err(code="ehr_error", message=str(e), retryable=True)
    return Ok(
        value={
            "slots": [
                {
                    "slot_id": s["id"],
                    "start_at_iso": s["start_at"],
                    "end_at_iso": s["end_at"],
                    "provider_id": s["provider_id"],
                    "provider_name": s["provider_name"],
                }
                for s in slots
            ]
        }
    )


async def create_appointment_handler(
    client: EHRClient,
    *,
    patient_id: str,
    slot_id: str,
    notes: str | None = None,
) -> Result[dict[str, Any]]:
    try:
        appt = await client.create_appointment(patient_id=patient_id, slot_id=slot_id, notes=notes)
    except EHRHTTPError as e:
        if (
            e.status_code == 409
            and isinstance(e.detail, dict)
            and e.detail.get("code") == "slot_taken"
        ):
            return Err(code="slot_taken_other_patient", message=str(e), retryable=True)
        if e.status_code == 404:
            return Err(code="patient_or_slot_not_found", message=str(e), retryable=False)
        return Err(code="ehr_error", message=str(e), retryable=True)
    return Ok(
        value={
            "appointment_id": appt["id"],
            "start_at": appt["start_at"],
            "end_at": appt["end_at"],
            "provider_name": appt["provider_name"],
        }
    )


async def get_upcoming_appointments_handler(
    client: EHRClient, *, patient_id: str
) -> Result[dict[str, Any]]:
    try:
        appts = await client.get_upcoming_appointments(patient_id)
    except EHRHTTPError as e:
        return Err(code="ehr_error", message=str(e), retryable=True)
    return Ok(
        value={
            "appointments": [
                {
                    "id": a["id"],
                    "start_at": a["start_at"],
                    "end_at": a["end_at"],
                    "provider_name": a["provider_name"],
                }
                for a in appts
            ]
        }
    )


async def cancel_appointment_handler(
    client: EHRClient, *, appointment_id: str, reason: str | None = None
) -> Result[dict[str, Any]]:
    try:
        cancelled = await client.cancel_appointment(appointment_id=appointment_id, reason=reason)
    except EHRHTTPError as e:
        if e.status_code == 404:
            return Err(code="appointment_not_found", message=str(e), retryable=False)
        return Err(code="ehr_error", message=str(e), retryable=True)
    return Ok(value={"ok": True, "appointment_id": cancelled["id"]})


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "find_patient_by_phone": {
        "type": "function",
        "function": {
            "name": "find_patient_by_phone",
            "description": (
                "Look up an existing patient by phone number. Returns "
                "{found, patients}. Use this BEFORE asking for name+DOB."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {
                        "type": "string",
                        "description": "Phone in any format; digits only is fine.",
                    }
                },
                "required": ["phone"],
            },
        },
    },
    "find_patient_by_name_dob": {
        "type": "function",
        "function": {
            "name": "find_patient_by_name_dob",
            "description": (
                "Fallback lookup when phone search fails. Returns {found, patients[similarity]}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "dob": {
                        "type": "string",
                        "description": (
                            "ISO 8601 date (YYYY-MM-DD) or unambiguous written "
                            "form (e.g. '1992-04-03', 'April 3 1992'). Strict "
                            "parsing: convert spoken forms before calling."
                        ),
                    },
                },
                "required": ["name", "dob"],
            },
        },
    },
    "create_patient": {
        "type": "function",
        "function": {
            "name": "create_patient",
            "description": (
                "Register a new patient. Only call after confirming details aloud with the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                    "dob": {"type": "string"},
                    "phone": {"type": "string"},
                    "email": {"type": "string"},
                },
                "required": ["first_name", "last_name", "dob", "phone"],
            },
        },
    },
    "list_availability_slots": {
        "type": "function",
        "function": {
            "name": "list_availability_slots",
            "description": "Return available 30-minute slots for a given date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": (
                            "YYYY-MM-DD (preferred) or unambiguous written "
                            "date. Convert spoken forms before calling."
                        ),
                    },
                    "provider_id": {"type": "string"},
                },
                "required": ["date"],
            },
        },
    },
    "create_appointment": {
        "type": "function",
        "function": {
            "name": "create_appointment",
            "description": (
                "Book the chosen slot for the identified patient. Only call "
                "after explicit user confirmation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_id": {"type": "string"},
                    "slot_id": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["patient_id", "slot_id"],
            },
        },
    },
    "get_upcoming_appointments": {
        "type": "function",
        "function": {
            "name": "get_upcoming_appointments",
            "description": (
                "List upcoming appointments for an identified patient. Use in CANCEL_FLOW."
            ),
            "parameters": {
                "type": "object",
                "properties": {"patient_id": {"type": "string"}},
                "required": ["patient_id"],
            },
        },
    },
    "cancel_appointment": {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": (
                "Cancel an appointment by id. Only call after explicit user confirmation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["appointment_id"],
            },
        },
    },
}


HANDLERS: dict[str, ToolHandler] = {
    "find_patient_by_phone": find_patient_by_phone_handler,
    "find_patient_by_name_dob": find_patient_by_name_dob_handler,
    "create_patient": create_patient_handler,
    "list_availability_slots": list_availability_slots_handler,
    "create_appointment": create_appointment_handler,
    "get_upcoming_appointments": get_upcoming_appointments_handler,
    "cancel_appointment": cancel_appointment_handler,
}
