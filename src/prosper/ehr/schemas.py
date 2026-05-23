"""Pydantic request/response shapes for the EHR HTTP API."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class PatientCreate(BaseModel):
    # Max lengths track the ORM column widths in models.py + a defensive cap
    # on phone/email so a malicious POST can't dump a multi-MB string into
    # the DB. SQLite would happily accept it; the DB columns truncate but
    # the request body is parsed entirely into memory before the cap fires.
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    dob: date
    phone: str = Field(min_length=7, max_length=32)
    email: str | None = Field(default=None, max_length=200)


class PatientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    first_name: str
    last_name: str
    dob: date
    phone: str
    email: str | None = None


class PatientWithSimilarity(PatientOut):
    similarity: float


class PatientList(BaseModel):
    patients: list[PatientOut]


class PatientFuzzyList(BaseModel):
    patients: list[PatientWithSimilarity]


class SlotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    provider_id: str
    provider_name: str
    start_at: datetime
    end_at: datetime


class SlotList(BaseModel):
    slots: list[SlotOut]


class AppointmentCreate(BaseModel):
    patient_id: str = Field(min_length=1, max_length=36)
    slot_id: str = Field(min_length=1, max_length=36)
    notes: str | None = Field(default=None, max_length=500)


class AppointmentCancel(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class AppointmentReschedule(BaseModel):
    new_slot_id: str = Field(min_length=1, max_length=36)


class AppointmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    patient_id: str
    slot_id: str
    status: str
    start_at: datetime
    end_at: datetime
    provider_id: str
    provider_name: str
    notes: str | None = None


class AppointmentList(BaseModel):
    appointments: list[AppointmentOut]
