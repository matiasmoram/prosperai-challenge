"""Coverage-gap tests for ``prosper.ehr.repository`` error branches."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from prosper.ehr import repository as repo
from prosper.ehr.models import AppointmentStatus, Base, Provider, Slot


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


# ---------------------------------------------------------------------------
# Bug-fuzz regressions
# ---------------------------------------------------------------------------


def test_normalize_phone_empty_returns_empty_not_plus() -> None:
    """Junk-only input must collapse to '' so the API min_length check rejects it."""
    assert repo.normalize_phone("") == ""
    assert repo.normalize_phone("abc") == ""
    # A bare '+' with no digits previously slipped through as '+' (length 1).
    assert repo.normalize_phone("+") == ""


def test_normalize_name_falls_back_to_unicode_for_pure_non_ascii() -> None:
    """A purely non-ASCII name must not collapse to '' (would collide in the index)."""
    arabic = repo.normalize_name("مَريم")
    cjk = repo.normalize_name("李雷")
    assert arabic != ""
    assert cjk != ""
    assert arabic != cjk  # distinct names must produce distinct normalised values


def test_normalize_name_strips_bidi_marks_without_crashing() -> None:
    """Bidi format characters (LRM/RLM/RLO) must be stripped, not crash NFKD."""
    for raw in ("Mary‎Sue", "Mary‏Sue", "Mary‮Sue"):
        assert repo.normalize_name(raw) == "marysue"


def test_cancel_appointment_is_idempotent(session: Session) -> None:
    """Second cancel of an already-cancelled appt must NOT mutate timestamps/notes."""
    prov = Provider(name="Dr. Idempotent", timezone="UTC")
    session.add(prov)
    session.commit()
    slot = Slot(
        provider_id=prov.id,
        start_at=datetime.now(timezone.utc) + timedelta(days=1),
        end_at=datetime.now(timezone.utc) + timedelta(days=1, minutes=30),
    )
    session.add(slot)
    session.commit()
    p = repo.create_patient(
        session,
        first_name="A",
        last_name="B",
        dob=date(1990, 1, 1),
        phone="+15551110001",
    )
    appt = repo.create_appointment(session, patient_id=p.id, slot_id=slot.id)

    first = repo.cancel_appointment(session, appointment_id=appt.id, reason="first")
    first_ts = first.cancelled_at
    first_notes = first.notes

    second = repo.cancel_appointment(session, appointment_id=appt.id, reason="second")
    assert second.status is AppointmentStatus.CANCELLED
    assert second.cancelled_at == first_ts, "second cancel must NOT overwrite cancelled_at"
    assert second.notes == first_notes, "second cancel must NOT append another note"
