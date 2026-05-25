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

import difflib
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

from dateutil import parser as dateparser

from prosper.ehr_client import EHRClient, EHRHTTPError
from prosper.prompts import SPECIALTY_DURATION_TABLE
from prosper.result import Err, Ok, Result

ToolHandler = Callable[..., Awaitable[Result[dict[str, Any]]]]

# HYBRID navigation tool. Whitelisted in ``flows.ALLOWED_TOOLS`` for
# CHOOSE_INTENT but deliberately ABSENT from ``HANDLERS`` — the dispatcher
# intercepts it (``Dispatcher._handle_route_intent``) because it manipulates
# FSM state, not the EHR. Named here so dispatcher + tests share one string.
ROUTE_INTENT_TOOL: str = "route_intent"

# Front-desk handoff tool. Whitelisted in ``flows.ALLOWED_TOOLS`` for the four
# post-identity flow states but deliberately ABSENT from ``HANDLERS`` — the
# dispatcher intercepts it (``Dispatcher._handle_leave_message``) because it
# needs ``SessionMemory`` (identity comes from the verified caller, not the LLM
# args) and the injected ``MailStore``, not the EHR client.
LEAVE_MESSAGE_TOOL: str = "leave_message_for_front_desk"

# Defensive bounds for any parsed date used downstream — DOBs and availability
# query dates alike. Catches obviously-wrong values (year 9999 typos, dateutil
# fuzzy-parser inventing 1990 from a stray digit) before they hit the DB.
_MIN_PARSED_YEAR = 1900
_MAX_PARSED_YEAR = 2100

# Two sentinel defaults that differ in EVERY component. dateutil fills any
# date field missing from the input with the default; parsing the same string
# against both reveals which fields were absent (they differ between parses).
# Used by ``_parse_dob`` to reject partial/ambiguous dates instead of silently
# completing them to today (audit F-001).
_DOB_DEFAULT_A = datetime(2000, 1, 1)
_DOB_DEFAULT_B = datetime(2001, 2, 2)

# Map spoken-number words to their digit. ElevenLabs realtime STT (no smart-
# format flag) frequently transcribes phone numbers as words ("five five five
# oh one zero zero"). LLM is asked to normalise but slips; this is the belt-
# and-braces normaliser at the tool boundary.
_NUMBER_WORDS: dict[str, str] = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "for": "4",  # frequent STT slip ("five-for-six" instead of "five-four-six")
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}


def _phone_words_to_digits(raw: str) -> str:
    """Convert any number-words inside a phone string to digits.

    Leaves digits untouched, drops other tokens. Returns the original string
    when the conversion would produce fewer than 7 digits — the EHR layer
    treats <7 digits as empty, so a bad conversion would mask the real input.
    """
    if not isinstance(raw, str):
        return raw
    lowered = [t.lower() for t in re.findall(r"[A-Za-z]+|\d+", raw)]

    def _is_spoken_digit(idx: int) -> bool:
        # A spoken number-word neighbour (zero..nine), excluding the ambiguous
        # "for" itself — used to decide whether a "for" is really the digit 4.
        return 0 <= idx < len(lowered) and lowered[idx] in _NUMBER_WORDS and lowered[idx] != "for"

    out: list[str] = []
    for i, low in enumerate(lowered):
        if low.isdigit():
            out.append(low)
        elif low == "for":
            # "for" is both the STT slip of "four" AND a ubiquitous English
            # filler. Only treat it as 4 when flanked by spoken number-words
            # ("five-for-six" → 546); adjacent to a literal digit run it is
            # filler and would splice a spurious 4 into a valid number (F-002).
            if _is_spoken_digit(i - 1) and _is_spoken_digit(i + 1):
                out.append("4")
        elif low in _NUMBER_WORDS:
            out.append(_NUMBER_WORDS[low])
        # else: silently drop non-number alphabetic noise
    digits = "".join(out)
    if len(digits) < 7:
        return raw
    return digits


# Canonical specialty values the EHR filters on — the provider-type nouns stored
# in `providers.specialty` (== the SPECIALTY_DURATION_TABLE keys). Single source
# of truth so adding a specialty in one place is enough.
_CANONICAL_SPECIALTIES: tuple[str, ...] = tuple(SPECIALTY_DURATION_TABLE.keys())


def _normalize_specialty(value: str | None) -> str | None:
    """Resolve caller/LLM specialty wording onto a canonical EHR specialty.

    The menu the bot reads aloud uses the friendly *service* noun ("Dermatology",
    "Therapy") while the EHR stores the *provider-type* noun ("Dermatologist",
    "Therapist") and filters it with a case-insensitive EXACT match. The LLM
    routinely passes the word it just offered, so the filter matches nothing and
    the bot loops "no slots" on a specialty that is actually wide open (live bug
    session f8bc099d: Dermatology). This belt-and-braces normaliser (mirrors
    ``_phone_words_to_digits``) fuzzy-maps the wording onto the nearest canonical
    value — "Dermatology"/"dermatology"/"derm" → "Dermatologist". It returns the
    raw value unchanged when nothing is close enough, so a genuinely unknown
    specialty still flows through to the EHR's empty/unknown path.
    """
    if not value or not isinstance(value, str):
        return value
    stripped = value.strip()
    lowered = stripped.lower()
    canon_by_lower = {c.lower(): c for c in _CANONICAL_SPECIALTIES}
    if lowered in canon_by_lower:  # exact (case-insensitive) hit
        return canon_by_lower[lowered]
    match = difflib.get_close_matches(lowered, list(canon_by_lower), n=1, cutoff=0.6)
    return canon_by_lower[match[0]] if match else stripped


def _parse_dob(raw: str) -> Result[date]:
    # `fuzzy=True` previously made the parser silently extract a year from
    # arbitrary text ("hello 1990" → 1990-05-20). `fuzzy=False` stops that,
    # but dateutil STILL fills any missing year/month/day component from a
    # default (datetime.now() by default), so "March" / "15" / "3pm" used to
    # resolve to today (audit F-001). We pass two sentinel defaults that differ
    # in every component and parse twice: any component absent from the input
    # is taken from the (differing) default, so the two parses disagree there.
    # A disagreement => the input was partial/ambiguous => fail loudly so the
    # LLM re-asks for a full year-month-day.
    if not isinstance(raw, str) or not raw.strip():
        return Err(code="dob_unparseable", message=f"empty date input: {raw!r}", retryable=True)
    try:
        parsed_a = dateparser.parse(raw, dayfirst=False, fuzzy=False, default=_DOB_DEFAULT_A)
        parsed_b = dateparser.parse(raw, dayfirst=False, fuzzy=False, default=_DOB_DEFAULT_B)
    except (ValueError, TypeError, AttributeError, OverflowError) as e:
        return Err(code="dob_unparseable", message=f"could not parse '{raw}': {e}", retryable=True)
    if parsed_a is None or parsed_b is None:
        return Err(code="dob_unparseable", message=f"could not parse '{raw}'", retryable=True)
    if (parsed_a.year, parsed_a.month, parsed_a.day) != (
        parsed_b.year,
        parsed_b.month,
        parsed_b.day,
    ):
        return Err(
            code="dob_unparseable",
            message=f"ambiguous or partial date '{raw}' — need a full year, month, and day",
            retryable=True,
        )
    parsed = parsed_a.date()
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
    phone = _phone_words_to_digits(phone)
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
    phone = _phone_words_to_digits(phone)
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
            # Echo the validated DOB back so the dispatcher's
            # patient_identified event carries dob_year for a
            # newly-registered caller — otherwise the operator console
            # shows "DOB —" on the handoff card for new patients.
            "dob": dob_r.value.isoformat(),
        }
    )


async def suggest_specialty_handler(
    client: EHRClient,  # noqa: ARG001 — kept for handler-signature uniformity; mini-LLM uses its own OpenAI client
    *,
    symptoms: str,
) -> Result[dict[str, Any]]:
    """Map a free-form symptom description to (specialty, duration_minutes).

    Wraps the mini-LLM JSON-mode call in ``llm.classify_symptoms``. The
    main LLM is expected to call this BEFORE ``list_availability_slots``
    when the caller described symptoms instead of naming a specialty.
    """
    # Lazy import — ``prosper.llm`` pulls in ``prosper.dispatcher`` which
    # in turn imports this module. Resolving at call time avoids the
    # circular import that an in-line module-level import would create.
    from prosper.llm import classify_symptoms

    r = await classify_symptoms(symptoms=symptoms)
    if r.kind == "err":
        return r
    cls = r.value
    if cls.red_flag:
        return Err(
            code="medical_emergency",
            message=(
                "Symptoms suggest a possible medical emergency. The agent "
                "must redirect the caller to 911 or emergency services and "
                "must NOT proceed with a routine booking."
            ),
            retryable=False,
        )
    return Ok(
        value={
            "specialty": cls.specialty,
            "duration_minutes": cls.duration_minutes,
            "minimum_safe_minutes": cls.minimum_safe_minutes,
            "rationale": cls.rationale,
            "confidence": cls.confidence,
            "follow_up": cls.follow_up,
        }
    )


async def list_availability_slots_handler(
    client: EHRClient,
    *,
    date: str,
    provider_id: str | None = None,
    specialty: str | None = None,
    duration_minutes: int = 30,
) -> Result[dict[str, Any]]:
    d_r = _parse_dob(date)
    if d_r.kind == "err":
        return Err(code="date_unparseable", message=d_r.message, retryable=True)
    asked = d_r.value
    if duration_minutes not in (30, 60, 90):
        return Err(
            code="invalid_duration",
            message=f"duration_minutes={duration_minutes} not in (30, 60, 90)",
            retryable=True,
        )
    # Map caller wording ("Dermatology") onto the canonical EHR value
    # ("Dermatologist") before any query — both the primary call and the
    # forward-scan probes below reuse this `specialty`. See `_normalize_specialty`.
    specialty = _normalize_specialty(specialty)
    try:
        slots = await client.list_availability(
            date_=asked,
            provider_id=provider_id,
            specialty=specialty,
            duration_minutes=duration_minutes,
        )
    except EHRHTTPError as e:
        return Err(code="ehr_error", message=str(e), retryable=True)

    # Auto-scan forward up to 6 days when the asked date is empty so the LLM
    # never gets a bare "no slots" dead-end. Surface both the asked date
    # (empty) and the next available date so the bot can say "Wednesday's
    # booked solid — Thursday has openings at 10 and 2, either work?". The
    # specialty + duration filter is preserved on probes so we don't suggest
    # a 30-min GP slot to a caller who needs a 60-min therapy session.
    next_day_with_slots: dict[str, Any] | None = None
    if not slots:
        from datetime import timedelta

        for offset in range(1, 7):
            probe = asked + timedelta(days=offset)
            try:
                probe_slots = await client.list_availability(
                    date_=probe,
                    provider_id=provider_id,
                    specialty=specialty,
                    duration_minutes=duration_minutes,
                )
            except EHRHTTPError:
                continue
            if probe_slots:
                next_day_with_slots = {
                    "date": probe.isoformat(),
                    "slots": [
                        {
                            "slot_id": s["id"],
                            "start_at_iso": s["start_at"],
                            "end_at_iso": s["end_at"],
                            "provider_id": s["provider_id"],
                            "provider_name": s["provider_name"],
                        }
                        for s in probe_slots
                    ],
                }
                break

    value: dict[str, Any] = {
        "asked_date": asked.isoformat(),
        # total_returned lets the dispatcher/LLM gauge abundance and pick the
        # right UX: few → read 2-3 options; many → invert and ask the caller
        # to narrow rather than dumping a long list.
        "total_returned": len(slots),
        "slots": [
            {
                "slot_id": s["id"],
                "start_at_iso": s["start_at"],
                "end_at_iso": s["end_at"],
                "provider_id": s["provider_id"],
                "provider_name": s["provider_name"],
            }
            for s in slots
        ],
    }
    if next_day_with_slots is not None:
        next_day_with_slots["total_returned"] = len(next_day_with_slots["slots"])
        value["next_day_with_slots"] = next_day_with_slots
    return Ok(value=value)


async def create_appointment_handler(
    client: EHRClient,
    *,
    patient_id: str,
    slot_id: str,
    duration_minutes: int = 30,
    notes: str | None = None,
) -> Result[dict[str, Any]]:
    if duration_minutes not in (30, 60, 90):
        return Err(
            code="invalid_duration",
            message=f"duration_minutes={duration_minutes} not in (30, 60, 90)",
            retryable=True,
        )
    try:
        appt = await client.create_appointment(
            patient_id=patient_id,
            slot_id=slot_id,
            duration_minutes=duration_minutes,
            notes=notes,
        )
    except EHRHTTPError as e:
        if (
            e.status_code == 409
            and isinstance(e.detail, dict)
            and e.detail.get("code") == "slot_taken"
        ):
            return Err(code="slot_taken_other_patient", message=str(e), retryable=True)
        if (
            e.status_code == 409
            and isinstance(e.detail, dict)
            and e.detail.get("code") == "no_consecutive_slots"
        ):
            return Err(code="no_consecutive_slots", message=str(e), retryable=True)
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


async def reschedule_appointment_handler(
    client: EHRClient, *, appointment_id: str, slot_id: str
) -> Result[dict[str, Any]]:
    """Atomic single-txn appointment slot swap.

    Replaces the older cancel-and-rebook chain — if the new slot is held
    by someone else, the original appointment is preserved (no orphan
    state). The LLM passes the new slot's bracketed handle as ``slot_id``
    just like ``create_appointment``; the dispatcher resolves it to the
    real UUID before calling here.
    """
    try:
        appt = await client.reschedule_appointment(
            appointment_id=appointment_id, new_slot_id=slot_id
        )
    except EHRHTTPError as e:
        if (
            e.status_code == 409
            and isinstance(e.detail, dict)
            and e.detail.get("code") == "slot_taken"
        ):
            return Err(code="slot_taken_other_patient", message=str(e), retryable=True)
        if e.status_code == 404:
            return Err(code="appointment_or_slot_not_found", message=str(e), retryable=False)
        return Err(code="ehr_error", message=str(e), retryable=True)
    return Ok(
        value={
            "appointment_id": appt["id"],
            "start_at": appt["start_at"],
            "end_at": appt["end_at"],
            "provider_name": appt["provider_name"],
        }
    )


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "route_intent": {
        "type": "function",
        "function": {
            "name": "route_intent",
            "description": (
                "Tell the system which thing the caller wants to do next so "
                "the conversation moves to the right step. Call this as soon "
                "as the caller's intent is clear in CHOOSE_INTENT — you cannot "
                "navigate yourself.\n\n"
                "Classification rules (apply in order; first match wins):\n"
                "- intent='reschedule': caller wants to MOVE or CHANGE an "
                "existing appointment to a different time — key words: "
                "'reschedule', 'move', 'change', 'switch', 'push back', "
                "'different time', 'different day', 'shift', 'adjust'. "
                "Reschedule keeps the visit but moves the slot; the old "
                "appointment stays until confirmed.\n"
                "- intent='cancel': caller wants to REMOVE an existing "
                "appointment entirely and NOT replace it — key words: "
                "'cancel', 'remove', 'delete', 'drop', 'take off', "
                "'won't make it', 'can't make it', 'don't want it'. "
                "Cancel means the appointment is gone with no new one.\n"
                "- intent='book': caller wants to schedule a NEW appointment "
                "they do not already have — key words: 'book', 'schedule', "
                "'new appointment', 'see a doctor', 'get in', 'sign up'.\n"
                "- intent='done': caller wants to end the call — "
                "'goodbye', 'done', 'that's all', 'hang up'.\n\n"
                "IMPORTANT: 'change my appointment', 'move my appointment', "
                "'switch to a different time', 'push it back' → "
                "ALWAYS use intent='reschedule', NEVER intent='cancel'. "
                "Only use intent='cancel' when the caller explicitly says "
                "they want to remove the appointment without rebooking.\n\n"
                "If the caller's wording is genuinely ambiguous (e.g. 'do "
                "something about my appointment', 'I need to deal with my "
                "visit') ask ONE short clarifying question — 'Would you like "
                "to reschedule that to a different time, or cancel it "
                "entirely?' — and do NOT call this tool until they answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "enum": ["book", "cancel", "reschedule", "done"],
                        "description": (
                            "The caller's intent. Use 'reschedule' for any "
                            "move/change/switch/push-back phrasing. Use 'cancel' "
                            "only when the caller explicitly wants to remove the "
                            "appointment without a replacement."
                        ),
                    },
                },
                "required": ["intent"],
            },
        },
    },
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
    "suggest_specialty": {
        "type": "function",
        "function": {
            "name": "suggest_specialty",
            "description": (
                "Map a caller's symptom description to the best-fit "
                "specialty AND a recommended visit duration in minutes. "
                "Call this BEFORE `list_availability_slots` if the caller "
                "described symptoms ('my stomach hurts', 'I've been "
                "feeling down') instead of naming a specialty. If they "
                "already said which provider they want (e.g. 'my "
                "therapist', 'a psychiatrist'), skip this tool and go "
                "straight to `list_availability_slots`. Returns "
                "{specialty, duration_minutes, minimum_safe_minutes, "
                "rationale, confidence, follow_up}. "
                "`duration_minutes` is the recommended length; "
                "`minimum_safe_minutes` is the clinical floor — see "
                "BOOK_FLOW task message for negotiation rules. "
                "If `follow_up` is set, ask it verbatim and call this "
                "tool again with the combined description."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symptoms": {
                        "type": "string",
                        "description": (
                            "Short description (≤200 chars) of what the "
                            "caller said is wrong. Paraphrase if needed; "
                            "do not invent symptoms."
                        ),
                    },
                },
                "required": ["symptoms"],
            },
        },
    },
    "list_availability_slots": {
        "type": "function",
        "function": {
            "name": "list_availability_slots",
            "description": (
                "Return available slots for a given date that can host a "
                "`duration_minutes` visit (30, 60, or 90). Optional "
                "`specialty` filter (e.g. 'Therapist', 'Psychiatrist', "
                "'General Practice', 'Dermatologist', 'Physiotherapist'). "
                "For 60- or 90-min visits the API only returns anchor "
                "slots whose next consecutive slot(s) are also free "
                "under the same provider, so picking any returned slot "
                "is always safe to book."
            ),
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
                    "specialty": {
                        "type": "string",
                        "description": (
                            "Filter by provider specialty (case-insensitive). "
                            "Leave empty if the caller did not specify."
                        ),
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "enum": [30, 60, 90],
                        "description": (
                            "Visit length. Default 30. Use the value the "
                            "previous `suggest_specialty` call returned, "
                            "or pick a sensible default per specialty "
                            "(GP/Dermatologist 30; Therapist/Psychiatrist 60)."
                        ),
                    },
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
                    "patient_id": {
                        "type": "string",
                        "description": (
                            "The identified patient's id. The dispatcher "
                            "auto-fills this from the call's verified "
                            "patient — you can pass an empty string if you "
                            "don't have one to hand."
                        ),
                    },
                    "slot_id": {
                        "type": "string",
                        "description": (
                            "The slot the caller picked. Pass the bracketed "
                            "number from the `list_availability_slots` "
                            "result — e.g. '1' for the first slot, '2' for "
                            "the second. Never invent UUIDs."
                        ),
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "enum": [30, 60, 90],
                        "description": (
                            "Visit length. MUST match what you passed to "
                            "`list_availability_slots` in this turn — the "
                            "EHR will reject a 60-min booking on a slot "
                            "whose adjacent block isn't free. Default 30."
                        ),
                    },
                    "notes": {
                        "type": "string",
                        "description": (
                            "Short reason for visit if the caller mentioned one "
                            "(e.g. 'follow-up', 'cough', 'medication review'). "
                            "Leave blank if not stated; do not invent."
                        ),
                    },
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
                    "appointment_id": {
                        "type": "string",
                        "description": (
                            "The appointment to cancel. Pass the bracketed "
                            "number from the `get_upcoming_appointments` "
                            "result — e.g. '1' for the first appointment, "
                            "'2' for the second. Never invent UUIDs.\n"
                            "If the caller identifies the appointment by "
                            "day ('the Tuesday one'), time ('the 2pm one'), "
                            "or provider ('the one with Dr. Chen'), match "
                            "it to the start_at / provider_name in the "
                            "numbered list and pass THAT number. If two or "
                            "more appointments match the description, read "
                            "them back and ask which one before calling."
                        ),
                    },
                    "reason": {"type": "string"},
                },
                "required": ["appointment_id"],
            },
        },
    },
    "reschedule_appointment": {
        "type": "function",
        "function": {
            "name": "reschedule_appointment",
            "description": (
                "Atomically move an existing appointment to a different "
                "available slot in a single transaction. Use this instead "
                "of cancel-then-rebook so the caller never loses their "
                "original appointment if the new slot turns out to be "
                "unavailable. Only call after explicit user confirmation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {
                        "type": "string",
                        "description": (
                            "Bracketed handle of the existing appointment "
                            "to move, from the `get_upcoming_appointments` "
                            "result — e.g. '1' for the first item, '2' for "
                            "the second. Never invent a UUID.\n"
                            "If the caller identifies the appointment by "
                            "day ('the Tuesday one'), time ('the 2pm one'), "
                            "or provider ('the one with Dr. Chen'), match "
                            "it to the start_at / provider_name in the "
                            "numbered list and pass THAT number. If two or "
                            "more appointments match the description, read "
                            "them back and ask which one before calling."
                        ),
                    },
                    "slot_id": {
                        "type": "string",
                        "description": (
                            "Bracketed handle of the NEW slot from the "
                            "`list_availability_slots` result — e.g. '2' "
                            "for the second slot offered. Never invent a "
                            "UUID."
                        ),
                    },
                },
                "required": ["appointment_id", "slot_id"],
            },
        },
    },
    # Dispatcher-intercepted tool: whitelisted so the LLM can call it, but
    # ABSENT from ``HANDLERS`` — the dispatcher handles it internally using
    # ``SessionMemory`` + ``MailStore`` (see ``Dispatcher._handle_leave_message``).
    "leave_message_for_front_desk": {
        "type": "function",
        "function": {
            "name": "leave_message_for_front_desk",
            "description": (
                "Hand the caller off to the human front desk by leaving a "
                "message for staff to follow up. Call this ONLY for things "
                "you cannot do yourself: prescription refills, "
                "insurance/billing questions, lab results/referrals/records, "
                "or when the caller explicitly asks to speak to a person. "
                "Do NOT call this for booking, cancelling, or rescheduling "
                "— do those yourself. NEVER call this for a medical "
                "emergency; for emergencies tell the caller to call 911 "
                "immediately. The patient's contact details are attached "
                "automatically from the verified caller."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [
                            "prescription",
                            "insurance_billing",
                            "records",
                            "medical_followup",
                            "other",
                        ],
                        "description": "Which kind of request this is.",
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "One short sentence for the front desk. "
                            "Paraphrase the caller's request; do not invent."
                        ),
                    },
                    "callback_wanted": {
                        "type": "boolean",
                        "description": "True if the caller wants someone to call them back.",
                    },
                },
                "required": ["category", "summary", "callback_wanted"],
            },
        },
    },
}


HANDLERS: dict[str, ToolHandler] = {
    "find_patient_by_phone": find_patient_by_phone_handler,
    "find_patient_by_name_dob": find_patient_by_name_dob_handler,
    "create_patient": create_patient_handler,
    "suggest_specialty": suggest_specialty_handler,
    "list_availability_slots": list_availability_slots_handler,
    "create_appointment": create_appointment_handler,
    "get_upcoming_appointments": get_upcoming_appointments_handler,
    "cancel_appointment": cancel_appointment_handler,
    "reschedule_appointment": reschedule_appointment_handler,
}


# Re-export the specialty/duration mapping so callers (dispatcher / tests /
# evals) can reach the canonical defaults without re-importing prompts.py.
__all__ = [
    "HANDLERS",
    "LEAVE_MESSAGE_TOOL",
    "ROUTE_INTENT_TOOL",
    "SPECIALTY_DURATION_TABLE",
    "TOOL_SCHEMAS",
    "ToolHandler",
    "cancel_appointment_handler",
    "create_appointment_handler",
    "create_patient_handler",
    "find_patient_by_name_dob_handler",
    "find_patient_by_phone_handler",
    "get_upcoming_appointments_handler",
    "list_availability_slots_handler",
    "reschedule_appointment_handler",
    "suggest_specialty_handler",
]
