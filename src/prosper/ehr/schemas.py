"""Pydantic request/response shapes for the EHR HTTP API.

Input-validation hardening (OWASP A03) lives here as field validators on the
request models: phone character-set guard, DOB plausibility bounds, and an
HTML/script strip on free-text fields. These run at the HTTP boundary before
any value reaches the repository or DB. They are intentionally *loose* where
the system relies on downstream normalisation — e.g. phone accepts
``(202) 555-0100`` and ``+12025550100`` alike (``repo.normalize_phone`` folds
them), but rejects anything carrying letters or angle brackets.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# A phone string may contain only digits and conventional grouping punctuation.
# This blocks injection payloads (letters, ``<``/``>``, quotes) while still
# accepting every format the front door normalises: "(202) 555-0100",
# "+1 202-555-0100", "2025550100", "202.555.0100".
_PHONE_ALLOWED = re.compile(r"^[\d\s()+.\-]+$")
# Minimum digit count for a usable phone number (NANP local is 7; the
# ``min_length`` Field cap counts characters, not digits, so a punctuation-
# heavy string could pass it with too few digits).
_MIN_PHONE_DIGITS = 7
# Earliest plausible birth year. Mirrors the parser bound in
# ``prosper.tools._parse_dob`` so the HTTP layer and the LLM-tool layer agree.
_MIN_DOB = date(1900, 1, 1)
# Strips any ``<...>`` tag so stored free-text can't carry HTML/script markup
# into a downstream renderer (the operator console, a future patient portal).
_HTML_TAG = re.compile(r"<[^>]+>")


def _strip_html(value: str | None) -> str | None:
    """Remove HTML/script tags from free-text; collapse an emptied value to None."""
    if value is None:
        return None
    cleaned = _HTML_TAG.sub("", value).strip()
    return cleaned or None


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

    @field_validator("phone")
    @classmethod
    def _validate_phone(cls, v: str) -> str:
        """Reject phones with disallowed characters or too few digits.

        Loose by design — normalisation happens in ``repo.normalize_phone``;
        this only blocks obviously-hostile or unusable input at the boundary.
        """
        if not _PHONE_ALLOWED.match(v):
            raise ValueError(
                "phone may contain only digits and the punctuation + - . ( ) space"
            )
        if sum(c.isdigit() for c in v) < _MIN_PHONE_DIGITS:
            raise ValueError(f"phone must contain at least {_MIN_PHONE_DIGITS} digits")
        return v

    @field_validator("dob")
    @classmethod
    def _validate_dob(cls, v: date) -> date:
        """Reject implausible birth dates: future, or before 1900-01-01."""
        if v > date.today():
            raise ValueError("date of birth cannot be in the future")
        if v < _MIN_DOB:
            raise ValueError("date of birth before 1900-01-01 is not supported")
        return v


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
    # Visit length in minutes; must be one of {30, 60, 90}. Repository's
    # CheckConstraint validates again at the DB boundary.
    duration_minutes: int = Field(default=30)
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("notes")
    @classmethod
    def _clean_notes(cls, v: str | None) -> str | None:
        """Strip HTML/script tags from the free-text reason-for-visit note."""
        return _strip_html(v)


class AppointmentCancel(BaseModel):
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, v: str | None) -> str | None:
        """Strip HTML/script tags from the free-text cancellation reason."""
        return _strip_html(v)


class AppointmentReschedule(BaseModel):
    new_slot_id: str = Field(min_length=1, max_length=36)
    # Optional duration override. ``None`` means "keep the current duration".
    new_duration_minutes: int | None = Field(default=None)


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
    duration_minutes: int = 30
    notes: str | None = None


class AppointmentList(BaseModel):
    appointments: list[AppointmentOut]
