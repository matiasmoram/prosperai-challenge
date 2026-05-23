"""SQLAlchemy ORM models for the EHR.

Schema is intentionally small: Provider owns Slots; Patient books
Appointments against Slots. ``AppointmentSlotLock`` is a one-row-per-slot
table whose primary key on ``slot_id`` provides DB-level protection
against double-booking even for multi-slot bookings (60- and 90-minute
visits lock 2 or 3 consecutive slots in a single transaction). The
partial unique index on ``Appointment.slot_id`` remains as a subset
backstop for the single-slot common case.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date as date_t
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    Date as SADate,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_str() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class AppointmentStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class Provider(Base):
    __tablename__ = "providers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    specialty: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        default="General Practice",
        server_default="General Practice",
        index=True,
    )
    slots: Mapped[list[Slot]] = relationship(back_populates="provider")


class Patient(Base):
    __tablename__ = "patients"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    dob: Mapped[date_t] = mapped_column(SADate, nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    appointments: Mapped[list[Appointment]] = relationship(back_populates="patient")


class Slot(Base):
    __tablename__ = "slots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    provider: Mapped[Provider] = relationship(back_populates="slots")
    appointment: Mapped[Appointment | None] = relationship(back_populates="slot", uselist=False)

    __table_args__ = (UniqueConstraint("provider_id", "start_at", name="uq_slot_provider_start"),)


class Appointment(Base):
    __tablename__ = "appointments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), index=True)
    # ``slot_id`` is the ANCHOR slot — the first 30-min block of a possibly
    # multi-slot booking. The full set of locked slots lives in
    # ``AppointmentSlotLock``. Reading code that surfaces "the start time of
    # the appointment" can keep using ``appointment.slot.start_at``.
    slot_id: Mapped[str] = mapped_column(ForeignKey("slots.id"), index=True)
    # Visit length in minutes. Grid is 30 min; allowed values today are
    # {30, 60, 90}. The CheckConstraint catches accidental writes — the
    # repository validates at write time and returns a typed Err earlier.
    duration_minutes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=30,
        server_default="30",
    )
    status: Mapped[AppointmentStatus] = mapped_column(
        SAEnum(AppointmentStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=AppointmentStatus.SCHEDULED,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)

    patient: Mapped[Patient] = relationship(back_populates="appointments")
    slot: Mapped[Slot] = relationship(back_populates="appointment")
    # All slots this appointment occupies (anchor + adjacent for 60/90 min).
    # cascade="all, delete-orphan" so an Appointment delete (rare —
    # cancel sets status, not delete) releases its locks too.
    locks: Mapped[list[AppointmentSlotLock]] = relationship(
        back_populates="appointment",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # Backward-compat subset guarantee for the single-slot path. The
        # load-bearing multi-slot guarantee is the PK on
        # ``appointment_slot_locks.slot_id``.
        Index(
            "uq_appointment_active_slot",
            "slot_id",
            unique=True,
            sqlite_where=text("status = 'scheduled'"),
        ),
        CheckConstraint(
            "duration_minutes IN (30, 60, 90)",
            name="ck_appointment_duration_minutes",
        ),
    )


class AppointmentSlotLock(Base):
    """One row per (slot, scheduled appointment) — DB-level multi-slot lock.

    For a 30-min appointment one row exists. For 60 min there are two
    consecutive rows (anchor + next slot, same provider). PK on ``slot_id``
    guarantees one scheduled appointment can hold a slot at any time, so a
    concurrent booker hits ``IntegrityError`` rather than silently
    double-booking. The repository translates that to ``SlotTakenError``
    and the API surfaces 409 ``slot_taken`` to the LLM.

    Lock rows are deleted in the same transaction as the cancel/reschedule
    they correspond to, so the slot becomes immediately bookable.
    """

    __tablename__ = "appointment_slot_locks"
    slot_id: Mapped[str] = mapped_column(ForeignKey("slots.id"), primary_key=True)
    appointment_id: Mapped[str] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False, index=True
    )

    appointment: Mapped[Appointment] = relationship(back_populates="locks")
