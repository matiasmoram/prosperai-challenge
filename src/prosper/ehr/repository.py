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
from sqlalchemy.orm import Session

from prosper.ehr.models import Appointment, AppointmentStatus, Patient, Slot

_HONORIFICS = {"mr", "mrs", "ms", "miss", "mx", "dr", "doctor", "prof", "professor"}


class SlotTakenError(Exception):
    """Raised when a slot is already booked by a different patient."""

    def __init__(self, *, slot_id: str, owner_patient_id: str) -> None:
        super().__init__(f"slot {slot_id} already booked by patient {owner_patient_id}")
        self.slot_id = slot_id
        self.owner_patient_id = owner_patient_id


class AppointmentNotFoundError(Exception):
    pass


def normalize_name(raw: str) -> str:
    """Lowercase, strip honorifics, collapse whitespace, drop diacritics."""
    decomposed = unicodedata.normalize("NFKD", raw)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower()
    tokens = [
        t for t in re.split(r"\s+", lowered.strip()) if t and t.rstrip(".") not in _HONORIFICS
    ]
    return " ".join(tokens)


def normalize_phone(raw: str) -> str:
    """E.164-ish normalisation. Strips non-digits, prefixes +1 for 10-digit US numbers."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if raw.startswith("+"):
        return "+" + digits
    return digits


def find_patient_by_phone(session: Session, phone: str) -> list[Patient]:
    normalized = normalize_phone(phone)
    return list(session.execute(select(Patient).where(Patient.phone == normalized)).scalars())


def find_patient_by_name_dob(
    session: Session,
    name: str,
    dob: date,
    *,
    min_similarity: float = 0.85,
) -> list[tuple[Patient, float]]:
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
    session.refresh(p)
    return p


def list_available_slots(
    session: Session,
    *,
    date_: date,
    provider_id: str | None = None,
) -> list[Slot]:
    day_start = datetime.combine(date_, time.min, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    stmt = (
        select(Slot)
        .where(Slot.start_at >= day_start)
        .where(Slot.start_at < day_end)
        .where(Slot.is_blocked.is_(False))
        .order_by(Slot.start_at)
    )
    if provider_id is not None:
        stmt = stmt.where(Slot.provider_id == provider_id)
    candidates = list(session.execute(stmt).scalars())
    return [
        s
        for s in candidates
        if not session.execute(
            select(Appointment)
            .where(Appointment.slot_id == s.id)
            .where(Appointment.status == AppointmentStatus.SCHEDULED)
        ).first()
    ]


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
    session.commit()
    session.refresh(appt)
    return appt


def cancel_appointment(
    session: Session,
    *,
    appointment_id: str,
    reason: str | None = None,
) -> Appointment:
    appt = session.get(Appointment, appointment_id)
    if appt is None:
        raise AppointmentNotFoundError(appointment_id)
    appt.status = AppointmentStatus.CANCELLED
    appt.cancelled_at = datetime.now(timezone.utc)
    if reason:
        appt.notes = (appt.notes + "\n" if appt.notes else "") + f"[cancel] {reason}"
    session.commit()
    session.refresh(appt)
    return appt


def get_upcoming_appointments(session: Session, *, patient_id: str) -> list[Appointment]:
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
