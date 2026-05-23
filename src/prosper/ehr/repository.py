"""Repository layer — all DB access lives here.

Functions accept a SQLAlchemy ``Session`` and return ORM objects or raise
typed exceptions. The FastAPI layer (api.py) translates exceptions to HTTP
status codes; the eval / dispatcher layers translate to ``Result[Ok, Err]``.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time, timedelta, timezone

from rapidfuzz.fuzz import token_sort_ratio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from prosper.ehr.models import Appointment, AppointmentStatus, Patient, Provider, Slot

_HONORIFICS = {"mr", "mrs", "ms", "miss", "mx", "dr", "doctor", "prof", "professor"}


class SlotTakenError(Exception):
    """Raised when a slot is already booked by a different patient."""

    def __init__(self, *, slot_id: str, owner_patient_id: str) -> None:
        super().__init__(f"slot {slot_id} already booked by patient {owner_patient_id}")
        self.slot_id = slot_id
        self.owner_patient_id = owner_patient_id


class AppointmentNotFoundError(Exception):
    """Raised when ``appointment_id`` matches no row in the appointments table."""


def normalize_name(raw: str) -> str:
    """Lowercase, strip honorifics, collapse whitespace, drop diacritics.

    If the NFKD+ASCII pass drops every character (e.g. pure-Arabic or pure-CJK
    names), fall back to the raw lowercased string with bidi/format marks
    removed — keeps the original Unicode codepoints so two distinct non-ASCII
    names don't both collapse to ``""`` and collide in the index.
    """
    decomposed = unicodedata.normalize("NFKD", raw)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower()
    tokens = [
        t for t in re.split(r"\s+", lowered.strip()) if t and t.rstrip(".") not in _HONORIFICS
    ]
    if tokens:
        return " ".join(tokens)
    # Fallback for non-Latin scripts: strip bidi/format chars, lowercase, collapse ws.
    stripped = "".join(ch for ch in raw if unicodedata.category(ch) != "Cf")
    return " ".join(stripped.lower().split())


def normalize_phone(raw: str) -> str:
    """E.164-ish normalisation. Strips non-digits, prefixes +1 for 10-digit US numbers.

    For obviously-empty input (no digits at all), returns ``""`` so the API
    layer's ``min_length=7`` check rejects it before it can be persisted. We
    do not over-validate here (still accept short/odd numbers — international
    formats vary) but we never return a bare ``"+"`` either.
    """
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if raw.startswith("+"):
        return "+" + digits
    return digits


def find_patient_by_phone(session: Session, phone: str) -> list[Patient]:
    """Exact-match lookup on the normalised phone column. Empty list if none."""
    normalized = normalize_phone(phone)
    return list(session.execute(select(Patient).where(Patient.phone == normalized)).scalars())


def find_patient_by_name_dob(
    session: Session,
    name: str,
    dob: date,
    *,
    min_similarity: float = 0.85,
) -> list[tuple[Patient, float]]:
    """Fuzzy-match candidates with matching DOB, sorted by similarity desc.

    Filters out any candidate whose token-sort ratio against ``name`` is below
    ``min_similarity``. Returns ``(patient, score)`` tuples so callers can
    show or threshold on confidence.
    """
    target = normalize_name(name)
    same_dob = list(session.execute(select(Patient).where(Patient.dob == dob)).scalars())
    scored: list[tuple[Patient, float]] = []
    for p in same_dob:
        sim = token_sort_ratio(target, p.name_normalized) / 100.0
        if sim >= min_similarity:
            scored.append((p, sim))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored


def create_patient(
    session: Session,
    *,
    first_name: str,
    last_name: str,
    dob: date,
    phone: str,
    email: str | None = None,
) -> Patient:
    """Insert a Patient row with normalised name + phone. Caller handles uniqueness."""
    p = Patient(
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        name_normalized=normalize_name(f"{first_name} {last_name}"),
        dob=dob,
        phone=normalize_phone(phone),
        email=email,
    )
    session.add(p)
    session.commit()
    # No session.refresh: expire_on_commit=False keeps `p` populated.
    # Saves ~0.7 ms per call.
    return p


def list_available_slots(
    session: Session,
    *,
    date_: date,
    provider_id: str | None = None,
    specialty: str | None = None,
) -> list[Slot]:
    """Slots on ``date_`` that are unblocked, unbooked, and not in the past.

    Optionally filtered to a single ``provider_id`` and/or a single
    ``specialty`` (case-insensitive exact match on ``Provider.specialty``).
    Returns rows ordered by ``start_at`` ascending.
    """
    day_start = datetime.combine(date_, time.min, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    # Audit A1: never offer a slot whose start_at is already in the past.
    cutoff = max(day_start, datetime.now(timezone.utc))
    # Single-query LEFT-OUTER-JOIN against scheduled appointments — drops the
    # previous N+1 (588 slot rows x 588 sub-selects = ~115ms on this dataset)
    # to a single index scan (~3ms).
    booked_subq = (
        select(Appointment.slot_id)
        .where(Appointment.status == AppointmentStatus.SCHEDULED)
        .scalar_subquery()
    )
    stmt = (
        select(Slot)
        .where(Slot.start_at >= cutoff)
        .where(Slot.start_at < day_end)
        .where(Slot.is_blocked.is_(False))
        .where(Slot.id.notin_(booked_subq))
        .order_by(Slot.start_at)
    )
    if provider_id is not None:
        stmt = stmt.where(Slot.provider_id == provider_id)
    if specialty is not None:
        # Case-insensitive specialty match via join so the index on
        # ``providers.specialty`` is usable. ``ilike`` lets the caller pass
        # "Therapist" / "therapist" interchangeably — the LLM is sloppy with
        # casing and we'd rather not lose a match because of it.
        stmt = stmt.join(Provider, Provider.id == Slot.provider_id).where(
            Provider.specialty.ilike(specialty)
        )
    return list(session.execute(stmt).scalars())


def list_providers(
    session: Session,
    *,
    specialty: str | None = None,
) -> list[Provider]:
    """All providers, optionally filtered to one ``specialty`` (case-insensitive)."""
    stmt = select(Provider).order_by(Provider.name)
    if specialty is not None:
        stmt = stmt.where(Provider.specialty.ilike(specialty))
    return list(session.execute(stmt).scalars())


def create_appointment(
    session: Session,
    *,
    patient_id: str,
    slot_id: str,
    notes: str | None = None,
) -> Appointment:
    """Idempotent: same patient + same slot returns existing row.

    Different patient on a held slot raises ``SlotTakenError``.
    """
    existing = session.execute(
        select(Appointment)
        .where(Appointment.slot_id == slot_id)
        .where(Appointment.status == AppointmentStatus.SCHEDULED)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.patient_id == patient_id:
            return existing
        raise SlotTakenError(slot_id=slot_id, owner_patient_id=existing.patient_id)
    appt = Appointment(patient_id=patient_id, slot_id=slot_id, notes=notes)
    session.add(appt)
    # Audit A2: a concurrent writer may have inserted a scheduled appointment
    # for this slot between our SELECT above and the COMMIT below. The partial
    # unique index catches it as an IntegrityError. Translate to SlotTakenError
    # so the EHR layer can emit a 409 (LLM-recoverable) instead of a 500.
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        race_owner = session.execute(
            select(Appointment)
            .where(Appointment.slot_id == slot_id)
            .where(Appointment.status == AppointmentStatus.SCHEDULED)
        ).scalar_one_or_none()
        owner_id = race_owner.patient_id if race_owner else "unknown"
        raise SlotTakenError(slot_id=slot_id, owner_patient_id=owner_id) from e
    return appt


def cancel_appointment(
    session: Session,
    *,
    appointment_id: str,
    reason: str | None = None,
) -> Appointment:
    """Mark a scheduled appointment as cancelled (idempotent). Appends ``reason`` to notes.

    Raises ``AppointmentNotFoundError`` if no row matches ``appointment_id``.
    Cancelling an already-cancelled appointment is a no-op (preserves the
    original ``cancelled_at`` + ``[cancel]`` audit trail).
    """
    appt = session.get(Appointment, appointment_id)
    if appt is None:
        raise AppointmentNotFoundError(appointment_id)
    # Idempotent: cancelling an already-cancelled appointment is a no-op.
    # Previously the second cancel would overwrite ``cancelled_at`` and append
    # a second "[cancel] ..." note — corrupting the audit trail.
    if appt.status == AppointmentStatus.CANCELLED:
        return appt
    appt.status = AppointmentStatus.CANCELLED
    appt.cancelled_at = datetime.now(timezone.utc)
    if reason:
        appt.notes = (appt.notes + "\n" if appt.notes else "") + f"[cancel] {reason}"
    session.commit()
    return appt


def reschedule_appointment(
    session: Session,
    *,
    appointment_id: str,
    new_slot_id: str,
) -> Appointment:
    """Atomically swap an appointment from its current slot to ``new_slot_id``.

    A single transaction updates ``appointment.slot_id`` so the caller can
    never end up with a cancelled old appointment AND a failed new booking.
    Raises ``AppointmentNotFoundError`` if the appointment is missing or
    already cancelled. Raises ``SlotTakenError`` if ``new_slot_id`` already
    holds a different patient's scheduled appointment (caught both via the
    pre-check and via the partial unique index on ``appointment.slot_id``).
    """
    appt = session.get(Appointment, appointment_id)
    if appt is None or appt.status != AppointmentStatus.SCHEDULED:
        raise AppointmentNotFoundError(appointment_id)
    # No-op fast path: caller asked to move the appointment to its current
    # slot. Return the existing row without a write — avoids surfacing a
    # phantom 409 from the unique index when the new id equals the old id.
    if appt.slot_id == new_slot_id:
        return appt
    holder = session.execute(
        select(Appointment)
        .where(Appointment.slot_id == new_slot_id)
        .where(Appointment.status == AppointmentStatus.SCHEDULED)
    ).scalar_one_or_none()
    if holder is not None and holder.id != appointment_id:
        raise SlotTakenError(slot_id=new_slot_id, owner_patient_id=holder.patient_id)
    appt.slot_id = new_slot_id
    try:
        session.commit()
    except IntegrityError as e:
        # Concurrent writer claimed ``new_slot_id`` between our SELECT and
        # the COMMIT. The partial unique index on ``appointment.slot_id``
        # (where status = scheduled) catches it; translate to a structured
        # error so the dispatcher returns ``slot_taken_other_patient``.
        session.rollback()
        race_owner = session.execute(
            select(Appointment)
            .where(Appointment.slot_id == new_slot_id)
            .where(Appointment.status == AppointmentStatus.SCHEDULED)
        ).scalar_one_or_none()
        owner_id = race_owner.patient_id if race_owner else "unknown"
        raise SlotTakenError(slot_id=new_slot_id, owner_patient_id=owner_id) from e
    return appt


def get_upcoming_appointments(session: Session, *, patient_id: str) -> list[Appointment]:
    """Future scheduled appointments for ``patient_id``, ordered by start time."""
    now = datetime.now(timezone.utc)
    stmt = (
        select(Appointment)
        .join(Slot, Slot.id == Appointment.slot_id)
        .where(Appointment.patient_id == patient_id)
        .where(Appointment.status == AppointmentStatus.SCHEDULED)
        .where(Slot.start_at >= now)
        .order_by(Slot.start_at)
    )
    return list(session.execute(stmt).scalars())
