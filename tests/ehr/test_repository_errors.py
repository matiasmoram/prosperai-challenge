"""Coverage-gap tests for ``prosper.ehr.repository`` error branches."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from prosper.ehr import repository as repo
from prosper.ehr.models import Base


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    s = Session(engine)
    yield s
    s.close()


def test_cancel_appointment_raises_when_id_unknown(session: Session) -> None:
    with pytest.raises(repo.AppointmentNotFoundError):
        repo.cancel_appointment(session, appointment_id="does-not-exist", reason=None)


def test_normalize_phone_handles_plus_prefix_already() -> None:
    """A phone the caller already prefixed with + should keep the +."""
    assert repo.normalize_phone("+44 7700 900123").startswith("+")


def test_normalize_phone_handles_seven_digit_short_number() -> None:
    """Short numbers (< 10 digits) are passed through as-is, no country prefix."""
    # Covers the "fall-through return" branch at the end of normalize_phone.
    assert repo.normalize_phone("5550123") == "5550123"


def test_normalize_name_strips_honorifics_and_diacritics() -> None:
    """Single-call check on the normalisation helper used in name+dob lookup."""
    assert repo.normalize_name("Dr. José  Martí") == "jose marti"
