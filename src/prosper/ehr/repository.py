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
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from prosper.ehr.models import (
    Appointment,
    AppointmentSlotLock,
    AppointmentStatus,
    Patient,
    Provider,
    Slot,
)

_HONORIFICS = {"mr", "mrs", "ms", "miss", "mx", "dr", "doctor", "prof", "professor"}

# Grid spacing of seeded slots. Visit durations are multiples of this; the
# repository validates duration_minutes against it before any DB write.
SLOT_GRID_MINUTES: int = 30
ALLOWED_DURATIONS: tuple[int, ...] = (30, 60, 90)


class SlotTakenError(Exception):
    """Raised when a slot is already booked by a different patient."""

    def __init__(self, *, slot_id: str, owner_patient_id: str) -> None:
        super().__init__(f"slot {slot_id} already booked by patient {owner_patient_id}")
        self.slot_id = slot_id
        self.owner_patient_id = owner_patient_id


class AppointmentNotFoundError(Exception):
    """Raised when ``appointment_id`` matches no row in the appointments table."""


class NoConsecutiveSlotsError(Exception):
    """Raised when a multi-slot booking can't find enough adjacent free slots.

    Carries ``anchor_slot_id`` (the slot the caller picked) and
    ``slots_needed`` so the EHR layer can return a structured 409 the LLM
    can recover from ("that 90-minute slot doesn't fit at 10:00 — try a
    different time?").
    """

    def __init__(self, *, anchor_slot_id: str, slots_needed: int) -> None:
        super().__init__(
            f"slot {anchor_slot_id} cannot host a {slots_needed * SLOT_GRID_MINUTES}-min "
            "appointment (adjacent slot missing, blocked, or already booked)"
        )
        self.anchor_slot_id = anchor_slot_id
        self.slots_needed = slots_needed


class InvalidDurationError(Exception):
    """Raised when ``duration_minutes`` is not one of ``ALLOWED_DURATIONS``."""

    def __init__(self, *, duration: int) -> None:
        super().__init__(f"duration_minutes={duration} not in {ALLOWED_DURATIONS}")
        self.duration = duration


def _slots_needed_for(duration_minutes: int) -> int:
    """Translate a visit duration to a count of 30-min grid blocks."""
    if duration_minutes not in ALLOWED_DURATIONS:
        raise InvalidDurationError(duration=duration_minutes)
    return duration_minutes // SLOT_GRID_MINUTES


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
    duration_minutes: int = 30,
) -> list[Slot]:
    """Slots on ``date_`` that can host a ``duration_minutes`` appointment.

    For 30-min visits returns every unblocked, unlocked, future slot on
    the date (optionally filtered by ``provider_id`` or ``specialty``).

    For 60- and 90-min visits, returns the ANCHOR slot of each
    contiguous run of N free same-provider slots where N =
    ``duration_minutes / 30``. A 60-min query returns slot S only when
    S+30 is also free under the same provider; a 90-min query needs S,
    S+30 and S+60. The caller books S as the anchor and the repository
    locks the adjacent slots atomically in ``create_appointment``.
    """
    slots_needed = _slots_needed_for(duration_minutes)
    day_start = datetime.combine(date_, time.min, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)
    # Audit A1: never offer a slot whose start_at is already in the past.
    cutoff = max(day_start, datetime.now(timezone.utc))
    locked_subq = select(AppointmentSlotLock.slot_id).scalar_subquery()
    stmt = (
        select(Slot)
        .where(Slot.start_at >= cutoff)
        .where(Slot.start_at < day_end)
        .where(Slot.is_blocked.is_(False))
        .where(Slot.id.notin_(locked_subq))
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
    candidates = list(session.execute(stmt).scalars())
    if slots_needed == 1:
        return candidates
    # Multi-slot: keep only anchors whose next (N-1) consecutive slots are
    # also unblocked, unlocked and same-provider. We do this by indexing the
    # full day's unlocked set into a (provider_id, start_at) → Slot map so
    # each anchor check is O(N-1) lookups instead of N-1 SQL round-trips.
    # The map covers a slightly wider window so a 90-min appointment
    # starting near end-of-day can still see the next morning's slot — but
    # we cap at "today's last_minute + slots_needed * grid" to keep the
    # working set small.
    horizon_end = day_end + timedelta(minutes=SLOT_GRID_MINUTES * slots_needed)
    horizon_stmt = (
        select(Slot)
        .where(Slot.start_at >= cutoff)
        .where(Slot.start_at < horizon_end)
        .where(Slot.is_blocked.is_(False))
        .where(Slot.id.notin_(locked_subq))
    )
    horizon: dict[tuple[str, datetime], Slot] = {
        (s.provider_id, s.start_at): s for s in session.execute(horizon_stmt).scalars()
    }
    bookable: list[Slot] = []
    for anchor in candidates:
        ok = True
        for step in range(1, slots_needed):
            next_start = anchor.start_at + timedelta(minutes=SLOT_GRID_MINUTES * step)
            if (anchor.provider_id, next_start) not in horizon:
                ok = False
                break
        if ok:
            bookable.append(anchor)
    return bookable


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


def _resolve_slot_chain(session: Session, *, anchor_slot_id: str, slots_needed: int) -> list[str]:
    """Return the chain of slot ids the booking will lock (anchor + adjacents).

    Raises ``NoConsecutiveSlotsError`` if any expected adjacent slot is
    missing, blocked, or already locked by another scheduled appointment.
    Validates *before* the INSERT so the caller sees a structured 409
    rather than an opaque IntegrityError on commit.
    """
    anchor = session.get(Slot, anchor_slot_id)
    if anchor is None:
        # Caller (api.py) usually checks slot existence first; defensive
        # fall-through here lets the EHR layer surface "slot_not_found".
        raise NoConsecutiveSlotsError(anchor_slot_id=anchor_slot_id, slots_needed=slots_needed)
    chain: list[str] = [anchor.id]
    for step in range(1, slots_needed):
        next_start = anchor.start_at + timedelta(minutes=SLOT_GRID_MINUTES * step)
        adjacent = session.execute(
            select(Slot)
            .where(Slot.provider_id == anchor.provider_id)
            .where(Slot.start_at == next_start)
            .where(Slot.is_blocked.is_(False))
        ).scalar_one_or_none()
        if adjacent is None:
            raise NoConsecutiveSlotsError(anchor_slot_id=anchor_slot_id, slots_needed=slots_needed)
        held = session.execute(
            select(AppointmentSlotLock).where(AppointmentSlotLock.slot_id == adjacent.id)
        ).scalar_one_or_none()
        if held is not None:
            raise NoConsecutiveSlotsError(anchor_slot_id=anchor_slot_id, slots_needed=slots_needed)
        chain.append(adjacent.id)
    return chain


def create_appointment(
    session: Session,
    *,
    patient_id: str,
    slot_id: str,
    duration_minutes: int = 30,
    notes: str | None = None,
) -> Appointment:
    """Idempotent: same patient + same anchor slot + same duration returns existing row.

    For ``duration_minutes`` > 30 locks the anchor PLUS the next
    ``(duration_minutes/30 - 1)`` adjacent slots under the same provider
    in a single transaction. Concurrent writers hit the
    ``appointment_slot_locks.slot_id`` PK constraint and surface as
    ``SlotTakenError``; missing-adjacent surfaces as
    ``NoConsecutiveSlotsError``.
    """
    slots_needed = _slots_needed_for(duration_minutes)
    existing = session.execute(
        select(Appointment)
        .where(Appointment.slot_id == slot_id)
        .where(Appointment.status == AppointmentStatus.SCHEDULED)
    ).scalar_one_or_none()
    if existing is not None:
        # Same patient already holds this anchor slot → idempotent return,
        # regardless of the requested duration. A caller can't hold two
        # appointments starting at the same slot; re-asking with a different
        # duration is a no-op, not a "taken by someone else" 409. (Changing
        # the duration of an existing booking is a reschedule, not a create.)
        if existing.patient_id == patient_id:
            return existing
        raise SlotTakenError(slot_id=slot_id, owner_patient_id=existing.patient_id)
    chain = _resolve_slot_chain(session, anchor_slot_id=slot_id, slots_needed=slots_needed)
    appt = Appointment(
        patient_id=patient_id,
        slot_id=slot_id,
        duration_minutes=duration_minutes,
        notes=notes,
    )
    session.add(appt)
    session.flush()  # populate appt.id so the lock rows can reference it
    for sid in chain:
        session.add(AppointmentSlotLock(slot_id=sid, appointment_id=appt.id))
    # Audit A2: a concurrent writer may have inserted a scheduled appointment
    # for any slot in the chain between our SELECT above and the COMMIT below.
    # The PK on appointment_slot_locks.slot_id catches it as IntegrityError.
    # Translate to SlotTakenError so the EHR layer can emit a 409
    # (LLM-recoverable) instead of a 500.
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        owner_id = "unknown"
        for sid in chain:
            race_lock = session.execute(
                select(AppointmentSlotLock).where(AppointmentSlotLock.slot_id == sid)
            ).scalar_one_or_none()
            if race_lock is not None:
                owning = session.get(Appointment, race_lock.appointment_id)
                if owning is not None:
                    owner_id = owning.patient_id
                    break
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
    # Free the slot(s) immediately so a follow-up booking in the same call
    # can re-use the same slot. Without this the slot stays locked until the
    # row is hard-deleted, which never happens — cancellation is soft.
    session.execute(
        delete(AppointmentSlotLock).where(AppointmentSlotLock.appointment_id == appointment_id)
    )
    session.commit()
    return appt


def reschedule_appointment(
    session: Session,
    *,
    appointment_id: str,
    new_slot_id: str,
    new_duration_minutes: int | None = None,
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
    target_duration = (
        new_duration_minutes if new_duration_minutes is not None else appt.duration_minutes
    )
    slots_needed = _slots_needed_for(target_duration)
    # No-op fast path: same anchor slot AND same duration. Skip the write
    # so the partial unique index can't spuriously 409 on (slot_id, status)
    # against itself when nothing actually changes.
    if appt.slot_id == new_slot_id and target_duration == appt.duration_minutes:
        return appt
    # Re-acquire the chain on the new anchor before touching anything.
    # ``_resolve_slot_chain`` raises NoConsecutiveSlotsError if any adjacent
    # block is missing, blocked, or already locked by someone else — but
    # the locks held by THIS appointment must not count as conflicts. We
    # release them first inside the same transaction so the chain check
    # sees the slots as free for re-acquisition. If the chain can't be
    # resolved we MUST roll back that delete+flush — otherwise the session
    # is left with the appointment pointing at its old anchor but with no
    # lock backing it (orphaned, double-bookable). Symmetric with
    # ``create_appointment``'s rollback-on-failure contract.
    session.execute(
        delete(AppointmentSlotLock).where(AppointmentSlotLock.appointment_id == appointment_id)
    )
    session.flush()
    try:
        chain = _resolve_slot_chain(session, anchor_slot_id=new_slot_id, slots_needed=slots_needed)
    except (NoConsecutiveSlotsError, SlotTakenError):
        # Undo the lock release so the original booking survives intact.
        session.rollback()
        raise
    appt.slot_id = new_slot_id
    appt.duration_minutes = target_duration
    for sid in chain:
        session.add(AppointmentSlotLock(slot_id=sid, appointment_id=appointment_id))
    try:
        session.commit()
    except IntegrityError as e:
        # Concurrent writer claimed one of the chain slots between our
        # SELECT and the COMMIT. PK on appointment_slot_locks.slot_id
        # catches it; translate to a structured 409.
        session.rollback()
        owner_id = "unknown"
        for sid in chain:
            race_lock = session.execute(
                select(AppointmentSlotLock).where(AppointmentSlotLock.slot_id == sid)
            ).scalar_one_or_none()
            if race_lock is not None and race_lock.appointment_id != appointment_id:
                owning = session.get(Appointment, race_lock.appointment_id)
                if owning is not None:
                    owner_id = owning.patient_id
                    break
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
