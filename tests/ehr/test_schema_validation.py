"""Input-validation hardening (OWASP A03) on the EHR request schemas.

Covers the field validators added to ``prosper.ehr.schemas``: phone
character-set + digit-count guard, DOB plausibility bounds, and HTML/script
stripping on free-text fields. These run at the HTTP boundary, so the tests
drive the live FastAPI app via TestClient where an end-to-end status code
matters, and the Pydantic models directly where a unit-level assertion is
clearer.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from prosper.ehr.api import create_app
from prosper.ehr.db import Base, get_engine
from prosper.ehr.models import Provider
from prosper.ehr.schemas import AppointmentCancel, AppointmentCreate, PatientCreate


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("PROSPER_DB_URL", f"sqlite:///{tmp_path / 'ehr.db'}")
    engine = get_engine(reset=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Provider(name="Dr. Patel", timezone="UTC"))
        session.commit()
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Phone validator — loose by design (normalisation happens downstream)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phone",
    [
        "(202) 555-0100",
        "+12025550100",
        "2025550100",
        "202.555.0100",
        "+1 202-555-0100",
    ],
)
def test_phone_accepts_all_normalisable_formats(phone: str) -> None:
    """Every format the front door normalises must pass validation."""
    p = PatientCreate(first_name="Ada", last_name="L", dob=date(1990, 1, 1), phone=phone)
    assert p.phone == phone  # validator is non-mutating; normalisation is later


@pytest.mark.parametrize(
    "phone",
    [
        "call me maybe",  # letters
        "<script>alert(1)</script>",  # injection markup
        "202-555-OHNO",  # letters mixed with digits
        "'; DROP TABLE patients;--",  # SQL-ish payload
    ],
)
def test_phone_rejects_letters_and_injection(phone: str) -> None:
    with pytest.raises(ValidationError):
        PatientCreate(first_name="Ada", last_name="L", dob=date(1990, 1, 1), phone=phone)


def test_phone_rejects_too_few_digits() -> None:
    """Punctuation-heavy strings that clear min_length but carry <7 digits fail."""
    with pytest.raises(ValidationError):
        PatientCreate(first_name="Ada", last_name="L", dob=date(1990, 1, 1), phone="()+ -.()")


def test_phone_validation_rejected_at_http_boundary(client: TestClient) -> None:
    r = client.post(
        "/patients",
        json={
            "first_name": "Mallory",
            "last_name": "X",
            "dob": "1990-01-01",
            "phone": "<script>alert(1)</script>",
        },
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# DOB plausibility — reject future + pre-1900
# ---------------------------------------------------------------------------


def test_dob_rejects_future_date() -> None:
    future = date.today() + timedelta(days=1)
    with pytest.raises(ValidationError):
        PatientCreate(first_name="Ada", last_name="L", dob=future, phone="2025550100")


def test_dob_rejects_before_1900() -> None:
    with pytest.raises(ValidationError):
        PatientCreate(
            first_name="Ada", last_name="L", dob=date(1899, 12, 31), phone="2025550100"
        )


def test_dob_accepts_1900_lower_boundary() -> None:
    """1900-01-01 is the inclusive floor — must be accepted."""
    p = PatientCreate(
        first_name="Ada", last_name="L", dob=date(1900, 1, 1), phone="2025550100"
    )
    assert p.dob == date(1900, 1, 1)


def test_dob_accepts_today() -> None:
    """A newborn registered the day they are born — today is inclusive."""
    p = PatientCreate(
        first_name="Baby", last_name="New", dob=date.today(), phone="2025550100"
    )
    assert p.dob == date.today()


def test_dob_future_rejected_at_http_boundary(client: TestClient) -> None:
    future = (date.today() + timedelta(days=365)).isoformat()
    r = client.post(
        "/patients",
        json={"first_name": "Time", "last_name": "Traveller", "dob": future, "phone": "2025550100"},
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# HTML/script strip on free-text notes + reason
# ---------------------------------------------------------------------------


def test_notes_strips_html_tags() -> None:
    appt = AppointmentCreate(
        patient_id="p1",
        slot_id="s1",
        notes="follow-up <script>steal()</script> on knee",
    )
    assert appt.notes == "follow-up steal() on knee"
    assert "<" not in (appt.notes or "")


def test_notes_all_markup_collapses_to_none() -> None:
    appt = AppointmentCreate(patient_id="p1", slot_id="s1", notes="<img src=x onerror=1>")
    assert appt.notes is None


def test_reason_strips_html_tags() -> None:
    cancel = AppointmentCancel(reason="moving <b>away</b>")
    assert cancel.reason == "moving away"


def test_notes_none_passes_through() -> None:
    appt = AppointmentCreate(patient_id="p1", slot_id="s1")
    assert appt.notes is None
