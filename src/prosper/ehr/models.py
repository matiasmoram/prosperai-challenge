"""SQLAlchemy ORM models for the EHR.

Schema is intentionally small: Provider owns Slots; Patient books
Appointments against Slots. A partial unique index on Appointment.slot_id
(where status = SCHEDULED) provides DB-level protection against
double-booking.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date as date_t
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
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
    slot_id: Mapped[str] = mapped_column(ForeignKey("slots.id"), index=True)
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

    __table_args__ = (
        Index(
            "uq_appointment_active_slot",
            "slot_id",
            unique=True,
            sqlite_where=text("status = 'scheduled'"),
        ),
    )
