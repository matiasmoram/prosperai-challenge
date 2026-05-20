"""Pydantic request/response shapes for the EHR HTTP API."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class PatientCreate(BaseModel):
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    dob: date
    phone: str = Field(min_length=7)
    email: str | None = None


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
    patient_id: str
    slot_id: str
    notes: str | None = None


class AppointmentCancel(BaseModel):
    reason: str | None = None


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
