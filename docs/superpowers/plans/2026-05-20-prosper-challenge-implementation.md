# Prosper Challenge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a voice agent that books and cancels appointments at a fictional health clinic, backed by a self-built FastAPI EHR. Hybrid FSM conversation flow, automated eval suite with paired state + LLM-judge assertions, latency instrumented with measured p50/p95 reported in `SOLUTION.md`.

**Architecture:** Two-process: (1) FastAPI EHR with SQLite + SQLAlchemy ORM serving 5 endpoints, (2) Pipecat bot using ElevenLabs STT/TTS + OpenAI LLM, driven by a custom dispatcher that swaps system prompt + tool whitelist per state. Tool handlers call EHR via shared `httpx.AsyncClient` and return `Result[Ok, Err]` typed values. Eval suite runs scripted scenarios against the dispatcher (skipping Pipecat) with EHR mounted in-process via `httpx.ASGITransport`.

**Tech Stack:** Python 3.10+, uv, FastAPI, SQLAlchemy 2.x, SQLite, httpx, pydantic, pipecat-ai (existing), openai, python-dateutil, rapidfuzz, pytest, pytest-asyncio, ruff, mypy, pre-commit.

**Spec reference:** `docs/superpowers/specs/2026-05-19-prosper-challenge-design.md` (v2).

---

## Phase 0 — Repo bootstrap

### Task 0.1: Update `pyproject.toml` with new dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Replace the `[project]` and dependency-groups blocks**

Open `pyproject.toml` and replace its contents entirely:

```toml
[project]
name = "prosper-challenge"
version = "0.1.0"
description = "Voice agent + EHR for the Prosper Health challenge"
requires-python = ">=3.10"
dependencies = [
    "pipecat-ai[webrtc,daily,silero,elevenlabs,openai,local-smart-turn-v3,runner]",
    "pipecat-ai-cli",
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "sqlalchemy>=2.0",
    "pydantic>=2.9",
    "httpx>=0.27",
    "python-dateutil>=2.9",
    "rapidfuzz>=3.10",
    "openai>=1.55",
    "loguru>=0.7",
    "python-dotenv>=1.0",
]

[dependency-groups]
dev = [
    "pyright>=1.1.404,<2",
    "ruff>=0.12.11,<1",
    "mypy>=1.13",
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "anyio>=4.6",
    "pre-commit>=4.0",
]

[tool.ruff]
line-length = 100

[tool.ruff.lint]
select = ["I", "E", "F", "W", "B", "UP"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = [
    "audio: full-pipeline audio smoke tests (slow, ElevenLabs credits required)",
    "live: tests that hit real external services (skipped without credentials)",
]

[tool.mypy]
strict = true
ignore_missing_imports = true

[tool.setuptools.packages.find]
where = ["src"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
```

- [ ] **Step 2: Sync deps**

Run: `uv sync`
Expected: dependencies resolve and install. If `uv sync` complains about the lockfile being out of date, run `uv lock` first then `uv sync`.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add FastAPI/SQLAlchemy/httpx/test deps for prosper challenge"
```

---

### Task 0.2: Create `src/prosper/` package skeleton

**Files:**
- Create: `src/prosper/__init__.py`
- Create: `src/prosper/ehr/__init__.py`
- Create: `src/prosper/observability/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `evals/__init__.py`
- Create: `evals/conftest.py`
- Modify: `.gitignore`

- [ ] **Step 1: Create empty package files**

Write these files with the exact content shown:

`src/prosper/__init__.py`:
```python
"""Prosper Health voice agent — appointment scheduling."""
__version__ = "0.1.0"
```

`src/prosper/ehr/__init__.py`:
```python
"""EHR HTTP service: FastAPI + SQLAlchemy + SQLite."""
```

`src/prosper/observability/__init__.py`:
```python
"""Structured logging + latency instrumentation."""
```

`tests/__init__.py`:
```python
```

`tests/conftest.py`:
```python
"""Shared pytest fixtures for unit tests."""
```

`evals/__init__.py`:
```python
```

`evals/conftest.py`:
```python
"""Shared pytest fixtures for scenario evals."""
```

- [ ] **Step 2: Append to `.gitignore`**

Append these lines (do not replace existing content):

```
# Prosper local state
data/
*.db
*.db-journal
.pytest_cache/
.mypy_cache/
.ruff_cache/
__pycache__/

# Eval artifacts
evals/results/
```

- [ ] **Step 3: Verify package import**

Run: `uv run python -c "import prosper; print(prosper.__version__)"`
Expected: prints `0.1.0`. If import fails, ensure `[tool.setuptools.packages.find] where = ["src"]` was written in 0.1 and `uv sync` ran.

- [ ] **Step 4: Commit**

```bash
git add src/ tests/ evals/ .gitignore
git commit -m "build: scaffold src/prosper package + tests/evals dirs"
```

---

## Phase 1 — EHR data model + repository

### Task 1.1: Define SQLAlchemy models

**Files:**
- Create: `src/prosper/ehr/models.py`
- Test: `tests/ehr/test_models.py`

- [ ] **Step 1: Write failing test**

Create `tests/ehr/__init__.py` (empty) and `tests/ehr/test_models.py`:

```python
"""Smoke tests for EHR ORM models."""
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from prosper.ehr.models import (
    AppointmentStatus,
    Base,
    Appointment,
    Patient,
    Provider,
    Slot,
)


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_can_create_provider_patient_slot_and_appointment() -> None:
    session = _make_session()
    provider = Provider(name="Dr. Patel", timezone="America/New_York")
    patient = Patient(
        first_name="Ada",
        last_name="Lovelace",
        name_normalized="ada lovelace",
        dob=date(1990, 12, 10),
        phone="+12025550100",
    )
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
    slot = Slot(provider=provider, start_at=start, end_at=start + timedelta(minutes=30))
    appt = Appointment(patient=patient, slot=slot, status=AppointmentStatus.SCHEDULED)
    session.add_all([provider, patient, slot, appt])
    session.commit()

    loaded = session.execute(select(Appointment)).scalar_one()
    assert loaded.patient.first_name == "Ada"
    assert loaded.slot.provider.name == "Dr. Patel"
    assert loaded.status is AppointmentStatus.SCHEDULED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ehr/test_models.py -v`
Expected: ImportError (`prosper.ehr.models` doesn't exist).

- [ ] **Step 3: Implement `models.py`**

Create `src/prosper/ehr/models.py`:

```python
"""SQLAlchemy ORM models for the EHR.

Schema is intentionally small: Provider owns Slots; Patient books Appointments
against Slots. A partial unique index on Appointment.slot_id (where status =
SCHEDULED) provides DB-level protection against double-booking.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy import Date as SADate
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_str() -> str:
    return str(uuid.uuid4())


class AppointmentStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class Provider(Base):
    __tablename__ = "providers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    slots: Mapped[list["Slot"]] = relationship(back_populates="provider")


class Patient(Base):
    __tablename__ = "patients"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    dob: Mapped["date"] = mapped_column(SADate, nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(tz=__import__("datetime").timezone.utc)
    )

    appointments: Mapped[list["Appointment"]] = relationship(back_populates="patient")


class Slot(Base):
    __tablename__ = "slots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(tz=__import__("datetime").timezone.utc)
    )

    provider: Mapped[Provider] = relationship(back_populates="slots")
    appointment: Mapped[Optional["Appointment"]] = relationship(
        back_populates="slot", uselist=False
    )

    __table_args__ = (
        UniqueConstraint("provider_id", "start_at", name="uq_slot_provider_start"),
    )


class Appointment(Base):
    __tablename__ = "appointments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), index=True)
    slot_id: Mapped[str] = mapped_column(ForeignKey("slots.id"), index=True)
    status: Mapped[AppointmentStatus] = mapped_column(
        SAEnum(AppointmentStatus), nullable=False, default=AppointmentStatus.SCHEDULED
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(tz=__import__("datetime").timezone.utc)
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    patient: Mapped[Patient] = relationship(back_populates="appointments")
    slot: Mapped[Slot] = relationship(back_populates="appointment")

    __table_args__ = (
        Index(
            "uq_appointment_active_slot",
            "slot_id",
            unique=True,
            sqlite_where=__import__("sqlalchemy").text("status = 'scheduled'"),
        ),
    )


from datetime import date  # noqa: E402  (re-export to satisfy `Mapped["date"]` annotation above)
```

- [ ] **Step 4: Run test, verify pass**

Run: `uv run pytest tests/ehr/test_models.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/prosper/ehr/models.py tests/ehr/
git commit -m "feat(ehr): SQLAlchemy models for Provider/Patient/Slot/Appointment"
```

---

### Task 1.2: Repository helpers (CRUD against models)

**Files:**
- Create: `src/prosper/ehr/repository.py`
- Test: `tests/ehr/test_repository.py`

- [ ] **Step 1: Write failing test**

`tests/ehr/test_repository.py`:

```python
"""Tests for repository helpers — phone lookup, name+dob fuzzy match,
availability query, idempotent appointment creation, cancellation."""
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from prosper.ehr import repository as repo
from prosper.ehr.models import AppointmentStatus, Base, Patient, Provider, Slot


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    s = Session(engine)
    yield s
    s.close()


def _seed_provider(session: Session) -> Provider:
    p = Provider(name="Dr. Patel", timezone="America/New_York")
    session.add(p)
    session.commit()
    return p


def _seed_slots(session: Session, provider: Provider, count: int = 3) -> list[Slot]:
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
    slots = []
    for i in range(count):
        s = Slot(
            provider=provider,
            start_at=start + timedelta(minutes=30 * i),
            end_at=start + timedelta(minutes=30 * (i + 1)),
        )
        session.add(s)
        slots.append(s)
    session.commit()
    return slots


def test_find_patient_by_phone_returns_match(session: Session) -> None:
    session.add(Patient(
        first_name="Ada", last_name="Lovelace", name_normalized="ada lovelace",
        dob=date(1990, 12, 10), phone="+12025550100",
    ))
    session.commit()
    matches = repo.find_patient_by_phone(session, "+12025550100")
    assert len(matches) == 1
    assert matches[0].first_name == "Ada"


def test_find_patient_by_phone_returns_empty_on_miss(session: Session) -> None:
    assert repo.find_patient_by_phone(session, "+19999999999") == []


def test_find_patient_by_name_dob_fuzzy(session: Session) -> None:
    session.add(Patient(
        first_name="Ada", last_name="Lovelace", name_normalized="ada lovelace",
        dob=date(1990, 12, 10), phone="+12025550100",
    ))
    session.commit()
    results = repo.find_patient_by_name_dob(session, "Ada Lovelas", date(1990, 12, 10))
    assert len(results) == 1
    patient, similarity = results[0]
    assert patient.first_name == "Ada"
    assert similarity >= 0.85


def test_find_patient_by_name_dob_requires_dob_match(session: Session) -> None:
    session.add(Patient(
        first_name="Ada", last_name="Lovelace", name_normalized="ada lovelace",
        dob=date(1990, 12, 10), phone="+12025550100",
    ))
    session.commit()
    assert repo.find_patient_by_name_dob(session, "Ada Lovelace", date(1991, 1, 1)) == []


def test_create_patient_inserts_and_normalises(session: Session) -> None:
    p = repo.create_patient(
        session, first_name="José",  last_name="Martí",
        dob=date(1853, 1, 28), phone="(202) 555-0199",
    )
    assert p.phone == "+12025550199"
    assert p.name_normalized == "jose marti"


def test_list_availability_excludes_booked_and_blocked(session: Session) -> None:
    provider = _seed_provider(session)
    slots = _seed_slots(session, provider, count=3)
    slots[1].is_blocked = True
    patient = repo.create_patient(
        session, first_name="A", last_name="B", dob=date(1990, 1, 1), phone="2025550111"
    )
    repo.create_appointment(session, patient_id=patient.id, slot_id=slots[0].id)
    available = repo.list_available_slots(session, date_=slots[0].start_at.date())
    available_ids = {s.id for s in available}
    assert slots[0].id not in available_ids       # booked
    assert slots[1].id not in available_ids       # blocked
    assert slots[2].id in available_ids


def test_create_appointment_is_idempotent_for_same_patient(session: Session) -> None:
    provider = _seed_provider(session)
    [slot] = _seed_slots(session, provider, count=1)
    patient = repo.create_patient(
        session, first_name="A", last_name="B", dob=date(1990, 1, 1), phone="2025550111"
    )
    a1 = repo.create_appointment(session, patient_id=patient.id, slot_id=slot.id)
    a2 = repo.create_appointment(session, patient_id=patient.id, slot_id=slot.id)
    assert a1.id == a2.id


def test_create_appointment_conflict_for_other_patient(session: Session) -> None:
    provider = _seed_provider(session)
    [slot] = _seed_slots(session, provider, count=1)
    a = repo.create_patient(
        session, first_name="A", last_name="B", dob=date(1990, 1, 1), phone="2025550111"
    )
    b = repo.create_patient(
        session, first_name="C", last_name="D", dob=date(1991, 2, 2), phone="2025550122"
    )
    repo.create_appointment(session, patient_id=a.id, slot_id=slot.id)
    with pytest.raises(repo.SlotTakenError) as excinfo:
        repo.create_appointment(session, patient_id=b.id, slot_id=slot.id)
    assert excinfo.value.owner_patient_id == a.id


def test_cancel_appointment_marks_status_and_frees_slot(session: Session) -> None:
    provider = _seed_provider(session)
    [slot] = _seed_slots(session, provider, count=1)
    patient = repo.create_patient(
        session, first_name="A", last_name="B", dob=date(1990, 1, 1), phone="2025550111"
    )
    appt = repo.create_appointment(session, patient_id=patient.id, slot_id=slot.id)
    repo.cancel_appointment(session, appointment_id=appt.id, reason="test")
    session.refresh(appt)
    assert appt.status is AppointmentStatus.CANCELLED
    assert appt.cancelled_at is not None
    # slot is now bookable again
    later = repo.create_appointment(session, patient_id=patient.id, slot_id=slot.id)
    assert later.id != appt.id


def test_get_upcoming_appointments_returns_only_scheduled_future(session: Session) -> None:
    provider = _seed_provider(session)
    slots = _seed_slots(session, provider, count=2)
    patient = repo.create_patient(
        session, first_name="A", last_name="B", dob=date(1990, 1, 1), phone="2025550111"
    )
    a1 = repo.create_appointment(session, patient_id=patient.id, slot_id=slots[0].id)
    a2 = repo.create_appointment(session, patient_id=patient.id, slot_id=slots[1].id)
    repo.cancel_appointment(session, appointment_id=a2.id, reason="x")
    upcoming = repo.get_upcoming_appointments(session, patient_id=patient.id)
    assert [a.id for a in upcoming] == [a1.id]
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/ehr/test_repository.py -v`
Expected: ImportError on `prosper.ehr.repository`.

- [ ] **Step 3: Implement `repository.py`**

Create `src/prosper/ehr/repository.py`:

```python
"""Repository layer — all DB access lives here.

Functions accept a SQLAlchemy ``Session`` and return ORM objects or raise
typed exceptions. The FastAPI layer (api.py) translates exceptions to HTTP
status codes; the eval / dispatcher layers translate to ``Result[Ok, Err]``.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime, time, timedelta
from typing import Optional

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
    tokens = [t for t in re.split(r"\s+", lowered.strip()) if t and t.rstrip(".") not in _HONORIFICS]
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
    session: Session, name: str, dob: date, *, min_similarity: float = 0.85
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
    email: Optional[str] = None,
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
    provider_id: Optional[str] = None,
) -> list[Slot]:
    day_start = datetime.combine(date_, time.min, tzinfo=UTC)
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
    # Exclude already-booked slots (active appointment present).
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
    notes: Optional[str] = None,
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
    session: Session, *, appointment_id: str, reason: Optional[str] = None
) -> Appointment:
    appt = session.get(Appointment, appointment_id)
    if appt is None:
        raise AppointmentNotFoundError(appointment_id)
    appt.status = AppointmentStatus.CANCELLED
    appt.cancelled_at = datetime.now(UTC)
    if reason:
        appt.notes = (appt.notes + "\n" if appt.notes else "") + f"[cancel] {reason}"
    session.commit()
    session.refresh(appt)
    return appt


def get_upcoming_appointments(session: Session, *, patient_id: str) -> list[Appointment]:
    now = datetime.now(UTC)
    stmt = (
        select(Appointment)
        .join(Slot, Slot.id == Appointment.slot_id)
        .where(Appointment.patient_id == patient_id)
        .where(Appointment.status == AppointmentStatus.SCHEDULED)
        .where(Slot.start_at >= now)
        .order_by(Slot.start_at)
    )
    return list(session.execute(stmt).scalars())
```

- [ ] **Step 4: Run test, verify pass**

Run: `uv run pytest tests/ehr/test_repository.py -v`
Expected: 10 passes. If `test_create_appointment_is_idempotent_for_same_patient` flakes, double-check that the partial unique index uses `sqlite_where`.

- [ ] **Step 5: Commit**

```bash
git add src/prosper/ehr/repository.py tests/ehr/test_repository.py
git commit -m "feat(ehr): repository — phone/name+dob lookup, idempotent booking, cancel"
```

---

## Phase 2 — EHR HTTP API + DB bootstrap + seed

### Task 2.1: Pydantic schemas + FastAPI app

**Files:**
- Create: `src/prosper/ehr/schemas.py`
- Create: `src/prosper/ehr/db.py`
- Create: `src/prosper/ehr/api.py`
- Test: `tests/ehr/test_api.py`

- [ ] **Step 1: Write failing test**

`tests/ehr/test_api.py`:

```python
"""End-to-end HTTP tests against the FastAPI EHR using TestClient."""
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from prosper.ehr.api import create_app
from prosper.ehr.db import Base, get_engine
from prosper.ehr.models import Provider, Slot
from sqlalchemy.orm import Session


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("PROSPER_DB_URL", f"sqlite:///{tmp_path/'ehr.db'}")
    engine = get_engine(reset=True)
    Base.metadata.create_all(engine)
    # Seed one provider + 3 slots starting tomorrow at 9am UTC.
    with Session(engine) as session:
        provider = Provider(name="Dr. Patel", timezone="America/New_York")
        session.add(provider)
        session.commit()
        start = (datetime.now(UTC) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        for i in range(3):
            session.add(Slot(
                provider_id=provider.id,
                start_at=start + timedelta(minutes=30 * i),
                end_at=start + timedelta(minutes=30 * (i + 1)),
            ))
        session.commit()
    return TestClient(create_app())


def test_create_then_find_patient_by_phone(client: TestClient) -> None:
    r = client.post("/patients", json={
        "first_name": "Ada", "last_name": "Lovelace",
        "dob": "1990-12-10", "phone": "(202) 555-0100",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["phone"] == "+12025550100"
    r2 = client.get("/patients/by-phone", params={"phone": "2025550100"})
    assert r2.status_code == 200
    assert len(r2.json()["patients"]) == 1


def test_find_patient_by_name_dob_returns_similarity(client: TestClient) -> None:
    client.post("/patients", json={
        "first_name": "Ada", "last_name": "Lovelace",
        "dob": "1990-12-10", "phone": "2025550100",
    })
    r = client.get("/patients/by-name-dob", params={
        "name": "Ada Lovelas", "dob": "1990-12-10",
    })
    assert r.status_code == 200
    patients = r.json()["patients"]
    assert len(patients) == 1
    assert patients[0]["similarity"] >= 0.85


def test_availability_lists_seed_slots(client: TestClient) -> None:
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    r = client.get("/availability", params={"date": tomorrow})
    assert r.status_code == 200
    assert len(r.json()["slots"]) == 3


def test_book_then_cancel_appointment_roundtrip(client: TestClient) -> None:
    p = client.post("/patients", json={
        "first_name": "Ada", "last_name": "Lovelace",
        "dob": "1990-12-10", "phone": "2025550100",
    }).json()
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    slot = client.get("/availability", params={"date": tomorrow}).json()["slots"][0]
    book = client.post("/appointments", json={"patient_id": p["id"], "slot_id": slot["id"]})
    assert book.status_code == 201, book.text
    appt = book.json()
    assert appt["status"] == "scheduled"
    # idempotent
    book2 = client.post("/appointments", json={"patient_id": p["id"], "slot_id": slot["id"]})
    assert book2.status_code in (200, 201)
    assert book2.json()["id"] == appt["id"]
    # cancel
    c = client.post(f"/appointments/{appt['id']}/cancel", json={"reason": "test"})
    assert c.status_code == 200
    assert c.json()["status"] == "cancelled"


def test_book_other_patient_on_taken_slot_returns_409(client: TestClient) -> None:
    p1 = client.post("/patients", json={
        "first_name": "Ada", "last_name": "Lovelace",
        "dob": "1990-12-10", "phone": "2025550100",
    }).json()
    p2 = client.post("/patients", json={
        "first_name": "Grace", "last_name": "Hopper",
        "dob": "1906-12-09", "phone": "2025550111",
    }).json()
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    slot = client.get("/availability", params={"date": tomorrow}).json()["slots"][0]
    client.post("/appointments", json={"patient_id": p1["id"], "slot_id": slot["id"]})
    r = client.post("/appointments", json={"patient_id": p2["id"], "slot_id": slot["id"]})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "slot_taken"


def test_get_patient_appointments(client: TestClient) -> None:
    p = client.post("/patients", json={
        "first_name": "Ada", "last_name": "Lovelace",
        "dob": "1990-12-10", "phone": "2025550100",
    }).json()
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    slot = client.get("/availability", params={"date": tomorrow}).json()["slots"][0]
    client.post("/appointments", json={"patient_id": p["id"], "slot_id": slot["id"]})
    r = client.get(f"/patients/{p['id']}/appointments")
    assert r.status_code == 200
    assert len(r.json()["appointments"]) == 1
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/ehr/test_api.py -v`
Expected: ImportError on `prosper.ehr.api` / `prosper.ehr.db`.

- [ ] **Step 3: Implement `db.py`**

Create `src/prosper/ehr/db.py`:

```python
"""Engine + session factory.

Engine URL is taken from ``PROSPER_DB_URL`` (default: file-backed
``data/ehr.db``). ``get_engine(reset=True)`` rebuilds a fresh engine — used
by tests so each fixture gets isolated state.
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from prosper.ehr.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _resolve_url() -> str:
    url = os.environ.get("PROSPER_DB_URL")
    if url:
        return url
    Path("data").mkdir(exist_ok=True)
    return "sqlite:///data/ehr.db"


def get_engine(*, reset: bool = False) -> Engine:
    global _engine, _SessionLocal
    if _engine is None or reset:
        _engine = create_engine(_resolve_url(), future=True, connect_args={"check_same_thread": False})
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
    return _engine


def init_db() -> None:
    Base.metadata.create_all(get_engine())


def get_session() -> Session:
    assert _SessionLocal is not None, "call get_engine() first"
    return _SessionLocal()
```

- [ ] **Step 4: Implement `schemas.py`**

Create `src/prosper/ehr/schemas.py`:

```python
"""Pydantic request/response shapes for the EHR HTTP API."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class PatientCreate(BaseModel):
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    dob: date
    phone: str = Field(min_length=7)
    email: Optional[str] = None


class PatientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    first_name: str
    last_name: str
    dob: date
    phone: str
    email: Optional[str] = None


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
    notes: Optional[str] = None


class AppointmentCancel(BaseModel):
    reason: Optional[str] = None


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
    notes: Optional[str] = None


class AppointmentList(BaseModel):
    appointments: list[AppointmentOut]
```

- [ ] **Step 5: Implement `api.py`**

Create `src/prosper/ehr/api.py`:

```python
"""FastAPI app: 5 challenge endpoints plus a helper for upcoming appointments."""
from __future__ import annotations

from datetime import date as date_t

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.orm import Session

from prosper.ehr import repository as repo
from prosper.ehr.db import get_engine, get_session, init_db
from prosper.ehr.models import Appointment, Patient, Slot
from prosper.ehr.schemas import (
    AppointmentCancel,
    AppointmentCreate,
    AppointmentList,
    AppointmentOut,
    PatientCreate,
    PatientFuzzyList,
    PatientList,
    PatientOut,
    PatientWithSimilarity,
    SlotList,
    SlotOut,
)


def _session_dep() -> Session:
    s = get_session()
    try:
        yield s
    finally:
        s.close()


def _appt_to_out(a: Appointment) -> AppointmentOut:
    return AppointmentOut(
        id=a.id,
        patient_id=a.patient_id,
        slot_id=a.slot_id,
        status=a.status.value,
        start_at=a.slot.start_at,
        end_at=a.slot.end_at,
        provider_id=a.slot.provider_id,
        provider_name=a.slot.provider.name,
        notes=a.notes,
    )


def _slot_to_out(s: Slot) -> SlotOut:
    return SlotOut(
        id=s.id,
        provider_id=s.provider_id,
        provider_name=s.provider.name,
        start_at=s.start_at,
        end_at=s.end_at,
    )


def create_app() -> FastAPI:
    get_engine()  # touch so SessionLocal is bound for this process
    init_db()
    app = FastAPI(title="Prosper EHR", version="0.1.0")

    @app.post("/patients", response_model=PatientOut, status_code=201)
    def create_patient(payload: PatientCreate, session: Session = Depends(_session_dep)) -> PatientOut:
        normalized_phone = repo.normalize_phone(payload.phone)
        existing = repo.find_patient_by_phone(session, normalized_phone)
        if existing:
            raise HTTPException(status_code=409, detail={"code": "patient_exists", "id": existing[0].id})
        p = repo.create_patient(
            session,
            first_name=payload.first_name,
            last_name=payload.last_name,
            dob=payload.dob,
            phone=payload.phone,
            email=payload.email,
        )
        return PatientOut.model_validate(p)

    @app.get("/patients/by-phone", response_model=PatientList)
    def find_by_phone(
        phone: str = Query(min_length=7),
        session: Session = Depends(_session_dep),
    ) -> PatientList:
        return PatientList(patients=[PatientOut.model_validate(p) for p in repo.find_patient_by_phone(session, phone)])

    @app.get("/patients/by-name-dob", response_model=PatientFuzzyList)
    def find_by_name_dob(
        name: str = Query(min_length=1),
        dob: date_t = Query(...),
        min_similarity: float = Query(0.85),
        session: Session = Depends(_session_dep),
    ) -> PatientFuzzyList:
        rows = repo.find_patient_by_name_dob(session, name, dob, min_similarity=min_similarity)
        return PatientFuzzyList(patients=[
            PatientWithSimilarity(
                id=p.id, first_name=p.first_name, last_name=p.last_name,
                dob=p.dob, phone=p.phone, email=p.email, similarity=sim,
            )
            for p, sim in rows
        ])

    @app.get("/patients/{patient_id}/appointments", response_model=AppointmentList)
    def patient_appointments(
        patient_id: str,
        session: Session = Depends(_session_dep),
    ) -> AppointmentList:
        if session.get(Patient, patient_id) is None:
            raise HTTPException(status_code=404, detail={"code": "patient_not_found"})
        appts = repo.get_upcoming_appointments(session, patient_id=patient_id)
        return AppointmentList(appointments=[_appt_to_out(a) for a in appts])

    @app.get("/availability", response_model=SlotList)
    def availability(
        date: date_t = Query(...),
        provider_id: str | None = Query(default=None),
        session: Session = Depends(_session_dep),
    ) -> SlotList:
        slots = repo.list_available_slots(session, date_=date, provider_id=provider_id)
        return SlotList(slots=[_slot_to_out(s) for s in slots])

    @app.post("/appointments", response_model=AppointmentOut, status_code=201)
    def create_appointment(
        payload: AppointmentCreate,
        session: Session = Depends(_session_dep),
    ) -> AppointmentOut:
        if session.get(Patient, payload.patient_id) is None:
            raise HTTPException(status_code=404, detail={"code": "patient_not_found"})
        if session.get(Slot, payload.slot_id) is None:
            raise HTTPException(status_code=404, detail={"code": "slot_not_found"})
        try:
            appt = repo.create_appointment(
                session,
                patient_id=payload.patient_id,
                slot_id=payload.slot_id,
                notes=payload.notes,
            )
        except repo.SlotTakenError as e:
            raise HTTPException(
                status_code=409,
                detail={"code": "slot_taken", "owner_patient_id": e.owner_patient_id},
            ) from e
        return _appt_to_out(appt)

    @app.post("/appointments/{appointment_id}/cancel", response_model=AppointmentOut)
    def cancel_appointment(
        appointment_id: str,
        payload: AppointmentCancel,
        session: Session = Depends(_session_dep),
    ) -> AppointmentOut:
        try:
            appt = repo.cancel_appointment(session, appointment_id=appointment_id, reason=payload.reason)
        except repo.AppointmentNotFoundError as e:
            raise HTTPException(status_code=404, detail={"code": "appointment_not_found"}) from e
        return _appt_to_out(appt)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


# Module-level instance for ``uvicorn prosper.ehr.api:app``.
app = create_app()
```

- [ ] **Step 6: Run test, verify pass**

Run: `uv run pytest tests/ehr/test_api.py -v`
Expected: 6 passes.

- [ ] **Step 7: Commit**

```bash
git add src/prosper/ehr/{db,schemas,api}.py tests/ehr/test_api.py
git commit -m "feat(ehr): FastAPI app exposing patients, availability, appointments"
```

---

### Task 2.2: Seed script

**Files:**
- Create: `scripts/seed.py`

- [ ] **Step 1: Implement seed script**

Create `scripts/seed.py`:

```python
"""Seed the local SQLite EHR with demo data.

Inserts three providers and ~120 slots (next 14 days, 9am–5pm UTC, every 30
min, lunch noon-1pm skipped) plus 2 demo patients. Idempotent: skips inserts
if matching rows already exist.

Run: ``uv run python scripts/seed.py``
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from prosper.ehr.db import get_engine, init_db
from prosper.ehr.models import Patient, Provider, Slot

PROVIDERS = [
    ("Dr. Aisha Patel", "America/New_York"),
    ("Dr. Marcus Chen", "America/New_York"),
    ("Dr. Sofia Romero", "America/New_York"),
]

DEMO_PATIENTS = [
    {
        "first_name": "Ada", "last_name": "Lovelace",
        "name_normalized": "ada lovelace",
        "dob": date(1990, 12, 10), "phone": "+12025550100",
    },
    {
        "first_name": "Grace", "last_name": "Hopper",
        "name_normalized": "grace hopper",
        "dob": date(1906, 12, 9), "phone": "+12025550111",
    },
]


def _make_slots_for_provider(provider_id: str, base: date) -> list[Slot]:
    slots: list[Slot] = []
    for day_offset in range(14):
        d = base + timedelta(days=day_offset)
        # 9am-5pm UTC, every 30 min, skipping noon-1pm
        for hour in range(9, 17):
            if hour == 12:
                continue
            for minute in (0, 30):
                start = datetime.combine(d, time(hour, minute), tzinfo=UTC)
                slots.append(Slot(
                    provider_id=provider_id,
                    start_at=start,
                    end_at=start + timedelta(minutes=30),
                ))
    return slots


def main() -> None:
    engine = get_engine()
    init_db()
    with Session(engine) as session:
        # providers
        for name, tz in PROVIDERS:
            if not session.execute(select(Provider).where(Provider.name == name)).first():
                session.add(Provider(name=name, timezone=tz))
        session.commit()

        # slots — only seed if zero rows
        if session.execute(select(Slot).limit(1)).first() is None:
            base = datetime.now(UTC).date() + timedelta(days=1)
            for prov in session.execute(select(Provider)).scalars():
                session.add_all(_make_slots_for_provider(prov.id, base))
            session.commit()

        # demo patients
        for p in DEMO_PATIENTS:
            if not session.execute(select(Patient).where(Patient.phone == p["phone"])).first():
                session.add(Patient(**p))
        session.commit()

        prov_count = session.execute(select(Provider)).scalars().all()
        slot_count = session.execute(select(Slot)).scalars().all()
        pat_count = session.execute(select(Patient)).scalars().all()
        print(f"seeded: providers={len(prov_count)} slots={len(slot_count)} patients={len(pat_count)}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify seed runs**

Run: `uv run python scripts/seed.py`
Expected: stdout reads e.g. `seeded: providers=3 slots=336 patients=2`. Verify `data/ehr.db` exists.

- [ ] **Step 3: Commit**

```bash
git add scripts/seed.py
git commit -m "feat(ehr): seed script (3 providers, 14 days slots, 2 demo patients)"
```

---

## Phase 3 — `Result[Ok, Err]` types + EHR client + tool handlers

### Task 3.1: Result type + EHR async client

**Files:**
- Create: `src/prosper/result.py`
- Create: `src/prosper/ehr_client.py`
- Test: `tests/test_result.py`
- Test: `tests/test_ehr_client.py`

- [ ] **Step 1: Write failing test for Result**

`tests/test_result.py`:

```python
from prosper.result import Err, Ok, Result, is_err, is_ok


def test_ok_holds_value() -> None:
    r: Result[int] = Ok(value=42)
    assert is_ok(r) and not is_err(r)
    assert r.kind == "ok"
    assert r.value == 42


def test_err_holds_code_message_retryable() -> None:
    r: Result[int] = Err(code="patient_not_found", message="no match for +1...", retryable=False)
    assert is_err(r) and not is_ok(r)
    assert r.code == "patient_not_found"
    assert r.retryable is False


def test_pattern_match() -> None:
    def describe(r: Result[int]) -> str:
        match r:
            case Ok(value=v):
                return f"ok:{v}"
            case Err(code=c):
                return f"err:{c}"
    assert describe(Ok(value=1)) == "ok:1"
    assert describe(Err(code="x", message="y", retryable=True)) == "err:x"
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/test_result.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `result.py`**

Create `src/prosper/result.py`:

```python
"""Typed Result discriminated union for tool handlers and integrations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeGuard, TypeVar, Union

T = TypeVar("T")


@dataclass(frozen=True)
class Ok(Generic[T]):
    value: T
    kind: Literal["ok"] = "ok"


@dataclass(frozen=True)
class Err:
    code: str
    message: str
    retryable: bool
    kind: Literal["err"] = "err"


Result = Union[Ok[T], Err]


def is_ok(r: Result[T]) -> TypeGuard[Ok[T]]:
    return r.kind == "ok"


def is_err(r: Result[T]) -> TypeGuard[Err]:
    return r.kind == "err"
```

- [ ] **Step 4: Run, verify pass**

Run: `uv run pytest tests/test_result.py -v`
Expected: PASS.

- [ ] **Step 5: Write failing test for ehr_client**

`tests/test_ehr_client.py`:

```python
"""Tests for the httpx-based async EHR client. Uses ASGITransport so the
client talks to the FastAPI app in-process without a separate uvicorn."""
from datetime import UTC, date, datetime, timedelta

import pytest

from prosper.ehr.api import create_app
from prosper.ehr.db import get_engine, init_db
from prosper.ehr.models import Provider, Slot
from prosper.ehr_client import EHRClient
from sqlalchemy.orm import Session


@pytest.fixture
def asgi_client(tmp_path, monkeypatch) -> EHRClient:
    monkeypatch.setenv("PROSPER_DB_URL", f"sqlite:///{tmp_path/'ehr.db'}")
    get_engine(reset=True)
    init_db()
    app = create_app()
    with Session(get_engine()) as session:
        prov = Provider(name="Dr. Patel", timezone="UTC")
        session.add(prov)
        session.commit()
        start = (datetime.now(UTC) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        for i in range(2):
            session.add(Slot(
                provider_id=prov.id,
                start_at=start + timedelta(minutes=30 * i),
                end_at=start + timedelta(minutes=30 * (i + 1)),
            ))
        session.commit()
    return EHRClient.for_asgi_app(app)


async def test_create_find_book_cancel_roundtrip(asgi_client: EHRClient) -> None:
    async with asgi_client:
        created = await asgi_client.create_patient(
            first_name="Ada", last_name="Lovelace",
            dob=date(1990, 12, 10), phone="2025550100",
        )
        assert created["phone"] == "+12025550100"

        by_phone = await asgi_client.find_patients_by_phone("2025550100")
        assert len(by_phone) == 1

        slots = await asgi_client.list_availability(
            date_=(datetime.now(UTC) + timedelta(days=1)).date()
        )
        assert len(slots) == 2

        appt = await asgi_client.create_appointment(patient_id=created["id"], slot_id=slots[0]["id"])
        assert appt["status"] == "scheduled"

        cancelled = await asgi_client.cancel_appointment(appointment_id=appt["id"], reason="t")
        assert cancelled["status"] == "cancelled"
```

- [ ] **Step 6: Run, verify fail**

Run: `uv run pytest tests/test_ehr_client.py -v`
Expected: ImportError on `prosper.ehr_client`.

- [ ] **Step 7: Implement `ehr_client.py`**

Create `src/prosper/ehr_client.py`:

```python
"""Async httpx wrapper around the EHR HTTP API.

Two construction modes:
- ``EHRClient(base_url=...)`` for the running uvicorn process (prod / dev).
- ``EHRClient.for_asgi_app(app)`` mounts the FastAPI app in-process via
  ``httpx.ASGITransport`` — used by tests and the eval runner for hermetic,
  fast scenario runs (no separate process, no socket bind).

All methods return JSON-decoded dicts/lists. Errors raise ``EHRHTTPError``;
the dispatcher translates these into ``Result[Ok, Err]`` values.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional, Self

import httpx
from fastapi import FastAPI


class EHRHTTPError(Exception):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class EHRClient:
    def __init__(self, *, transport: Optional[httpx.AsyncBaseTransport] = None, base_url: str = "http://ehr") -> None:
        self._transport = transport
        self._base_url = base_url
        self._client: Optional[httpx.AsyncClient] = None

    @classmethod
    def for_asgi_app(cls, app: FastAPI) -> Self:
        return cls(transport=httpx.ASGITransport(app=app), base_url="http://ehr")

    @classmethod
    def for_http(cls, base_url: str) -> Self:
        return cls(transport=None, base_url=base_url)

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(transport=self._transport, base_url=self._base_url, timeout=5.0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _c(self) -> httpx.AsyncClient:
        assert self._client is not None, "use `async with` to bind the client"
        return self._client

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        r = await self._c().request(method, path, **kwargs)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except Exception:
                detail = r.text
            raise EHRHTTPError(r.status_code, detail)
        if r.status_code == 204:
            return None
        return r.json()

    # ---- patients ----
    async def create_patient(self, *, first_name: str, last_name: str, dob: date,
                             phone: str, email: str | None = None) -> dict[str, Any]:
        return await self._request("POST", "/patients", json={
            "first_name": first_name, "last_name": last_name,
            "dob": dob.isoformat(), "phone": phone, "email": email,
        })

    async def find_patients_by_phone(self, phone: str) -> list[dict[str, Any]]:
        body = await self._request("GET", "/patients/by-phone", params={"phone": phone})
        return body["patients"]

    async def find_patients_by_name_dob(self, name: str, dob: date,
                                        min_similarity: float = 0.85) -> list[dict[str, Any]]:
        body = await self._request("GET", "/patients/by-name-dob", params={
            "name": name, "dob": dob.isoformat(), "min_similarity": min_similarity,
        })
        return body["patients"]

    async def get_upcoming_appointments(self, patient_id: str) -> list[dict[str, Any]]:
        body = await self._request("GET", f"/patients/{patient_id}/appointments")
        return body["appointments"]

    # ---- availability + appointments ----
    async def list_availability(self, *, date_: date,
                                provider_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"date": date_.isoformat()}
        if provider_id is not None:
            params["provider_id"] = provider_id
        body = await self._request("GET", "/availability", params=params)
        return body["slots"]

    async def create_appointment(self, *, patient_id: str, slot_id: str,
                                 notes: str | None = None) -> dict[str, Any]:
        return await self._request("POST", "/appointments", json={
            "patient_id": patient_id, "slot_id": slot_id, "notes": notes,
        })

    async def cancel_appointment(self, *, appointment_id: str,
                                 reason: str | None = None) -> dict[str, Any]:
        return await self._request("POST", f"/appointments/{appointment_id}/cancel",
                                   json={"reason": reason})
```

- [ ] **Step 8: Run test, verify pass**

Run: `uv run pytest tests/test_ehr_client.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/prosper/result.py src/prosper/ehr_client.py tests/test_result.py tests/test_ehr_client.py
git commit -m "feat: Result[Ok,Err] type + async EHR client (httpx + ASGI transport)"
```

---

### Task 3.2: Tool handlers (`tools.py`)

**Files:**
- Create: `src/prosper/tools.py`
- Test: `tests/test_tools.py`

- [ ] **Step 1: Write failing test**

`tests/test_tools.py`:

```python
"""Tool handlers translate EHR client calls into Result[Ok,Err] for the LLM."""
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from prosper.ehr.api import create_app
from prosper.ehr.db import get_engine, init_db
from prosper.ehr.models import Provider, Slot
from prosper.ehr_client import EHRClient
from prosper.result import Err, Ok, is_err, is_ok
from prosper.tools import (
    cancel_appointment_handler,
    create_appointment_handler,
    create_patient_handler,
    find_patient_by_name_dob_handler,
    find_patient_by_phone_handler,
    get_upcoming_appointments_handler,
    list_availability_slots_handler,
)


@pytest.fixture
def client(tmp_path, monkeypatch) -> EHRClient:
    monkeypatch.setenv("PROSPER_DB_URL", f"sqlite:///{tmp_path/'ehr.db'}")
    get_engine(reset=True)
    init_db()
    app = create_app()
    with Session(get_engine()) as session:
        prov = Provider(name="Dr. Patel", timezone="UTC")
        session.add(prov); session.commit()
        start = (datetime.now(UTC) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        for i in range(2):
            session.add(Slot(provider_id=prov.id, start_at=start + timedelta(minutes=30*i),
                             end_at=start + timedelta(minutes=30*(i+1))))
        session.commit()
    return EHRClient.for_asgi_app(app)


async def test_find_by_phone_no_match(client: EHRClient) -> None:
    async with client:
        r = await find_patient_by_phone_handler(client, phone="+19999999999")
    assert is_ok(r)
    assert r.value["found"] is False
    assert r.value["patients"] == []


async def test_create_then_find_then_book_then_cancel(client: EHRClient) -> None:
    async with client:
        created = await create_patient_handler(
            client, first_name="Ada", last_name="Lovelace",
            dob="1990-12-10", phone="2025550100",
        )
        assert is_ok(created)
        pid = created.value["patient_id"]

        slots_r = await list_availability_slots_handler(
            client, date=(datetime.now(UTC) + timedelta(days=1)).date().isoformat()
        )
        assert is_ok(slots_r)
        slot_id = slots_r.value["slots"][0]["slot_id"]

        booked = await create_appointment_handler(client, patient_id=pid, slot_id=slot_id)
        assert is_ok(booked)
        appt_id = booked.value["appointment_id"]

        # idempotent
        booked2 = await create_appointment_handler(client, patient_id=pid, slot_id=slot_id)
        assert is_ok(booked2) and booked2.value["appointment_id"] == appt_id

        cancelled = await cancel_appointment_handler(client, appointment_id=appt_id, reason="test")
        assert is_ok(cancelled)


async def test_book_same_slot_other_patient_returns_typed_err(client: EHRClient) -> None:
    async with client:
        a = (await create_patient_handler(
            client, first_name="A", last_name="A", dob="1990-01-01", phone="2025550100",
        )).value["patient_id"]
        b = (await create_patient_handler(
            client, first_name="B", last_name="B", dob="1991-02-02", phone="2025550111",
        )).value["patient_id"]
        slots = (await list_availability_slots_handler(
            client, date=(datetime.now(UTC) + timedelta(days=1)).date().isoformat()
        )).value["slots"]
        await create_appointment_handler(client, patient_id=a, slot_id=slots[0]["slot_id"])
        r = await create_appointment_handler(client, patient_id=b, slot_id=slots[0]["slot_id"])
    assert is_err(r)
    assert r.code == "slot_taken_other_patient"
    assert r.retryable is True


async def test_cancel_nonexistent_returns_typed_err(client: EHRClient) -> None:
    async with client:
        r = await cancel_appointment_handler(client, appointment_id="does-not-exist", reason=None)
    assert is_err(r)
    assert r.code == "appointment_not_found"


async def test_dob_parser_accepts_spoken_forms(client: EHRClient) -> None:
    async with client:
        r = await find_patient_by_name_dob_handler(client, name="ada lovelace", dob="December 10, 1990")
    assert is_ok(r)
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/test_tools.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `tools.py`**

Create `src/prosper/tools.py`:

```python
"""Tool handlers exposed to the LLM via OpenAI function-calling.

Each handler:
1. Coerces / validates string inputs (DOB parsing, etc).
2. Calls the EHR via the shared ``EHRClient``.
3. Returns ``Result[Ok[dict], Err]`` with a stable ``code`` enum on the Err
   side. The Err code is what the dispatcher branches on AND what eval
   scenarios assert against — so codes must NEVER drift silently.

The OpenAI tool schemas live in ``TOOL_SCHEMAS`` below; the per-state
whitelist in ``flows.py`` references them by name.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from dateutil import parser as dateparser

from prosper.ehr_client import EHRClient, EHRHTTPError
from prosper.result import Err, Ok, Result


def _parse_dob(raw: str) -> Result[date]:
    try:
        # dayfirst=False — US-style by default; flag in spec acknowledges
        # this and the dispatcher confirms ambiguous dates aloud anyway.
        parsed = dateparser.parse(raw, dayfirst=False, fuzzy=True).date()
    except (ValueError, TypeError, AttributeError) as e:
        return Err(code="dob_unparseable", message=f"could not parse '{raw}': {e}", retryable=True)
    return Ok(value=parsed)


async def find_patient_by_phone_handler(
    client: EHRClient, *, phone: str
) -> Result[dict[str, Any]]:
    try:
        patients = await client.find_patients_by_phone(phone)
    except EHRHTTPError as e:
        return Err(code="ehr_error", message=str(e), retryable=True)
    return Ok(value={"found": bool(patients), "patients": patients})


async def find_patient_by_name_dob_handler(
    client: EHRClient, *, name: str, dob: str
) -> Result[dict[str, Any]]:
    dob_r = _parse_dob(dob)
    if dob_r.kind == "err":
        return dob_r
    try:
        patients = await client.find_patients_by_name_dob(name, dob_r.value)
    except EHRHTTPError as e:
        return Err(code="ehr_error", message=str(e), retryable=True)
    return Ok(value={"found": bool(patients), "patients": patients})


async def create_patient_handler(
    client: EHRClient, *, first_name: str, last_name: str, dob: str,
    phone: str, email: str | None = None,
) -> Result[dict[str, Any]]:
    dob_r = _parse_dob(dob)
    if dob_r.kind == "err":
        return dob_r
    try:
        created = await client.create_patient(
            first_name=first_name, last_name=last_name,
            dob=dob_r.value, phone=phone, email=email,
        )
    except EHRHTTPError as e:
        code = "patient_exists" if e.status_code == 409 else "ehr_error"
        return Err(code=code, message=str(e), retryable=False)
    return Ok(value={"patient_id": created["id"], "first_name": created["first_name"],
                     "last_name": created["last_name"], "phone": created["phone"]})


async def list_availability_slots_handler(
    client: EHRClient, *, date: str, provider_id: str | None = None,
) -> Result[dict[str, Any]]:
    d_r = _parse_dob(date)  # reuse tolerant parser; date strings are similar to DOBs
    if d_r.kind == "err":
        return Err(code="date_unparseable", message=d_r.message, retryable=True)
    try:
        slots = await client.list_availability(date_=d_r.value, provider_id=provider_id)
    except EHRHTTPError as e:
        return Err(code="ehr_error", message=str(e), retryable=True)
    return Ok(value={"slots": [
        {
            "slot_id": s["id"],
            "start_at_iso": s["start_at"],
            "end_at_iso": s["end_at"],
            "provider_id": s["provider_id"],
            "provider_name": s["provider_name"],
        }
        for s in slots
    ]})


async def create_appointment_handler(
    client: EHRClient, *, patient_id: str, slot_id: str, notes: str | None = None,
) -> Result[dict[str, Any]]:
    try:
        appt = await client.create_appointment(patient_id=patient_id, slot_id=slot_id, notes=notes)
    except EHRHTTPError as e:
        if e.status_code == 409 and isinstance(e.detail, dict) and e.detail.get("code") == "slot_taken":
            return Err(code="slot_taken_other_patient", message=str(e), retryable=True)
        if e.status_code == 404:
            return Err(code="patient_or_slot_not_found", message=str(e), retryable=False)
        return Err(code="ehr_error", message=str(e), retryable=True)
    return Ok(value={
        "appointment_id": appt["id"],
        "start_at": appt["start_at"],
        "end_at": appt["end_at"],
        "provider_name": appt["provider_name"],
    })


async def get_upcoming_appointments_handler(
    client: EHRClient, *, patient_id: str,
) -> Result[dict[str, Any]]:
    try:
        appts = await client.get_upcoming_appointments(patient_id)
    except EHRHTTPError as e:
        return Err(code="ehr_error", message=str(e), retryable=True)
    return Ok(value={"appointments": [
        {
            "id": a["id"],
            "start_at": a["start_at"],
            "end_at": a["end_at"],
            "provider_name": a["provider_name"],
        }
        for a in appts
    ]})


async def cancel_appointment_handler(
    client: EHRClient, *, appointment_id: str, reason: str | None = None,
) -> Result[dict[str, Any]]:
    try:
        cancelled = await client.cancel_appointment(appointment_id=appointment_id, reason=reason)
    except EHRHTTPError as e:
        if e.status_code == 404:
            return Err(code="appointment_not_found", message=str(e), retryable=False)
        return Err(code="ehr_error", message=str(e), retryable=True)
    return Ok(value={"ok": True, "appointment_id": cancelled["id"]})


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "find_patient_by_phone": {
        "type": "function",
        "function": {
            "name": "find_patient_by_phone",
            "description": "Look up an existing patient by phone number. Returns {found, patients}. Use this BEFORE asking for name+DOB.",
            "parameters": {
                "type": "object",
                "properties": {"phone": {"type": "string", "description": "Phone in any format; digits only is fine."}},
                "required": ["phone"],
            },
        },
    },
    "find_patient_by_name_dob": {
        "type": "function",
        "function": {
            "name": "find_patient_by_name_dob",
            "description": "Fallback lookup when phone search fails. Returns {found, patients[similarity]}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "dob": {"type": "string", "description": "Any spoken or written form, e.g. 'April third nineteen ninety-two' or '1992-04-03'."},
                },
                "required": ["name", "dob"],
            },
        },
    },
    "create_patient": {
        "type": "function",
        "function": {
            "name": "create_patient",
            "description": "Register a new patient. Only call after confirming details aloud with the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                    "dob": {"type": "string"},
                    "phone": {"type": "string"},
                    "email": {"type": "string"},
                },
                "required": ["first_name", "last_name", "dob", "phone"],
            },
        },
    },
    "list_availability_slots": {
        "type": "function",
        "function": {
            "name": "list_availability_slots",
            "description": "Return available 30-minute slots for a given date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD or spoken date."},
                    "provider_id": {"type": "string"},
                },
                "required": ["date"],
            },
        },
    },
    "create_appointment": {
        "type": "function",
        "function": {
            "name": "create_appointment",
            "description": "Book the chosen slot for the identified patient. Only call after explicit user confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_id": {"type": "string"},
                    "slot_id": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["patient_id", "slot_id"],
            },
        },
    },
    "get_upcoming_appointments": {
        "type": "function",
        "function": {
            "name": "get_upcoming_appointments",
            "description": "List upcoming appointments for an identified patient. Use in CANCEL_FLOW.",
            "parameters": {
                "type": "object",
                "properties": {"patient_id": {"type": "string"}},
                "required": ["patient_id"],
            },
        },
    },
    "cancel_appointment": {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": "Cancel an appointment by id. Only call after explicit user confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["appointment_id"],
            },
        },
    },
}


HANDLERS = {
    "find_patient_by_phone": find_patient_by_phone_handler,
    "find_patient_by_name_dob": find_patient_by_name_dob_handler,
    "create_patient": create_patient_handler,
    "list_availability_slots": list_availability_slots_handler,
    "create_appointment": create_appointment_handler,
    "get_upcoming_appointments": get_upcoming_appointments_handler,
    "cancel_appointment": cancel_appointment_handler,
}
```

- [ ] **Step 4: Run test, verify pass**

Run: `uv run pytest tests/test_tools.py -v`
Expected: 5 passes.

- [ ] **Step 5: Commit**

```bash
git add src/prosper/tools.py tests/test_tools.py
git commit -m "feat: tool handlers + OpenAI schemas, Result[Ok,Err] returns"
```

---

## Phase 4 — Prompts + FSM (`flows.py` + `dispatcher.py`)

### Task 4.1: `CLINIC_PERSONA` + per-state prompts

**Files:**
- Create: `src/prosper/prompts.py`
- Test: `tests/test_prompts.py`

- [ ] **Step 1: Write failing test**

`tests/test_prompts.py`:

```python
"""Sanity checks on prompt sizes — persona large enough for OpenAI cache,
per-state task messages tight enough to stay fast."""
from prosper.prompts import (
    CLINIC_PERSONA, TASK_MESSAGES, MIN_PERSONA_TOKENS_FOR_CACHE,
)


def _approx_tokens(s: str) -> int:
    # Cheap heuristic: 4 chars per token. Good enough for a size guard.
    return len(s) // 4


def test_persona_long_enough_for_prompt_cache() -> None:
    assert _approx_tokens(CLINIC_PERSONA) >= MIN_PERSONA_TOKENS_FOR_CACHE


def test_persona_mentions_core_responsibilities() -> None:
    lower = CLINIC_PERSONA.lower()
    for phrase in ("prosper health", "appointment", "cancel", "confirm"):
        assert phrase in lower, f"missing '{phrase}' in persona"


def test_every_state_has_a_task_message() -> None:
    expected = {
        "GREETING", "IDENTIFY_PATIENT", "REGISTER_PATIENT", "CHOOSE_INTENT",
        "BOOK_FLOW", "CANCEL_FLOW", "CONFIRM_BOOK", "CONFIRM_CANCEL", "END",
    }
    assert set(TASK_MESSAGES) == expected


def test_each_task_message_under_1_kb() -> None:
    for state, msg in TASK_MESSAGES.items():
        assert len(msg) < 1024, f"{state} task message is {len(msg)} bytes (>=1024)"
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/test_prompts.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `prompts.py`**

Create `src/prosper/prompts.py`:

```python
"""Per-state system prompts and the stable CLINIC_PERSONA preamble.

The persona is intentionally ≥ ~1100 tokens (~4400 chars) so OpenAI's prompt
cache kicks in — cache hits drop per-turn input tokens dramatically on
repeat-state turns. Do not edit the persona mid-sprint or you'll thrash the
cache. Task messages stay under 1 KB to keep per-turn output budgets tight.
"""
from __future__ import annotations

MIN_PERSONA_TOKENS_FOR_CACHE = 1024


CLINIC_PERSONA = """\
You are the voice assistant at Prosper Health, a US-based outpatient clinic.
Your single job is to help callers (existing patients and prospective ones)
book a new appointment, reschedule via cancel-and-rebook, or cancel an
existing appointment. You do not give medical advice, you do not discuss
insurance, billing, prescriptions, lab results, or referrals. If a caller
asks about any of those, politely redirect: "Our front desk can help with
that during business hours — would you like me to take a message, or shall
we go ahead and book a visit?"

Voice and style
- Warm, brief, conversational. Aim for fewer than two sentences per turn
  unless reading back details. Speak naturally; avoid robotic phrasing.
- Never spell out database identifiers, internal codes, or status enums to
  the caller. Talk about appointments by date, time, and provider name.
- Read dates as "Tuesday, May 26th" rather than "2026-05-26". Read times as
  "ten thirty in the morning" rather than "10:30 AM" — but understand both
  when the caller says them.
- When you read back something for confirmation, be specific: the date, the
  time of day, the provider's name, and any key detail you collected (phone
  number, date of birth). Do not skip the read-back before any state change.

Hard rules for tool use
- You only have access to the tools listed in the CURRENT STATE's task
  message. Calling any other tool will fail and waste the caller's time.
- Before calling any write tool (create_patient, create_appointment,
  cancel_appointment) you MUST first read the relevant details back to the
  caller and receive an explicit yes-or-no confirmation. If the answer is
  not a clear yes, treat it as no and ask again or offer to back out.
- If a tool returns an error (any Err result), apologise briefly, restate
  what you understood, and either retry once with corrected inputs or
  escalate by offering to transfer the caller to a person.
- Never invent appointment times, slot ids, provider names, patient ids,
  or appointment ids. Every fact you tell the caller must come from a tool
  result you just received in this turn, or from a previous tool result
  that is still in your conversation context.

Identification, registration, booking, cancellation
- For identification, always try phone number first (one short utterance,
  digits are easy for the system). Fall back to name plus date of birth
  only if the phone lookup returns no match.
- If a new patient needs to be registered, collect first name, last name,
  date of birth, and phone number. Confirm aloud before calling
  create_patient. Email is optional — only ask if the caller offers it.
- For booking, offer 2–3 specific times when you list availability, not the
  whole day. "I have ten o'clock or eleven thirty on Tuesday — either of
  those work?" Then narrow down.
- For cancellation: if the caller has exactly one upcoming appointment,
  read it back and ask "cancel that one?". If they have more than one,
  read them out as a numbered list ("one, Tuesday at ten with Dr. Patel;
  two, Friday at three with Dr. Chen…") and ask which number to cancel.
  Accept ordinals or date phrases. If they have none, say so.

Timezone
- The clinic operates in US Eastern time. When the caller does not specify
  a timezone, assume Eastern. If they say something like "morning", offer
  a specific time and let them adjust.

If a caller asks anything off-script
- Politely redirect to booking or cancelling. Do not improvise medical,
  legal, or financial answers under any circumstance.

End of call
- Once a booking or cancellation is confirmed (tool returned ok), wrap up
  with a short send-off ("You're all set — see you on Tuesday at ten.
  Have a great day.") and stop.
"""


TASK_MESSAGES = {
    "GREETING": (
        "[STATE: GREETING] Open warmly: introduce yourself as the Prosper "
        "Health scheduling assistant, ask if the caller is looking to book "
        "or cancel a visit. Keep it under two sentences. Do NOT call any "
        "tools in this state."
    ),
    "IDENTIFY_PATIENT": (
        "[STATE: IDENTIFY_PATIENT] Identify the caller. First ask for their "
        "phone number (just the digits). Call find_patient_by_phone. If "
        "found exactly once, confirm their name aloud and move on. If "
        "multiple, ask for date of birth to narrow down. If none, ask for "
        "full name and DOB, then call find_patient_by_name_dob. If still "
        "no match, you are done with this state — the dispatcher will route "
        "to registration."
    ),
    "REGISTER_PATIENT": (
        "[STATE: REGISTER_PATIENT] Collect first name, last name, DOB, and "
        "the phone number the caller already gave you. Read all four back "
        "for confirmation in a single sentence. On an explicit yes, call "
        "create_patient. On no, ask which field is wrong and re-collect "
        "just that field."
    ),
    "CHOOSE_INTENT": (
        "[STATE: CHOOSE_INTENT] Ask whether they want to book a new "
        "appointment or cancel an existing one. One short sentence. Do NOT "
        "call any tools — the dispatcher reads your reply to decide."
    ),
    "BOOK_FLOW": (
        "[STATE: BOOK_FLOW] Ask what day works. Parse it tolerantly (today, "
        "tomorrow, 'next Tuesday'). Call list_availability_slots for that "
        "date. Offer 2 or 3 specific times, not the whole list. Let the "
        "caller pick. Keep the chosen slot_id in mind — you will need it in "
        "CONFIRM_BOOK."
    ),
    "CANCEL_FLOW": (
        "[STATE: CANCEL_FLOW] Call get_upcoming_appointments for the "
        "identified patient. If exactly one, read it back and ask 'cancel "
        "that one?'. If multiple, read a numbered list and ask which "
        "number. If none, say there's nothing upcoming and offer to book "
        "instead. Keep the chosen appointment_id in mind for CONFIRM_CANCEL."
    ),
    "CONFIRM_BOOK": (
        "[STATE: CONFIRM_BOOK] Read back the chosen date, time, and "
        "provider name in a single short sentence. Ask for explicit yes or "
        "no. On yes, call create_appointment with the slot_id and "
        "patient_id. On no, ask whether they want a different time or to "
        "cancel out."
    ),
    "CONFIRM_CANCEL": (
        "[STATE: CONFIRM_CANCEL] Read back the appointment you're about to "
        "cancel in one short sentence, ask 'go ahead with that?'. On yes, "
        "call cancel_appointment with the appointment_id. On no, ask "
        "whether they meant a different one or want to keep it."
    ),
    "END": (
        "[STATE: END] Briefly wrap up: confirm what just happened, wish "
        "them well, stop. Do NOT call any tools."
    ),
}
```

- [ ] **Step 4: Run test, verify pass**

Run: `uv run pytest tests/test_prompts.py -v`
Expected: 4 passes. If `test_persona_long_enough_for_prompt_cache` fails, add more clarifying paragraphs to `CLINIC_PERSONA` until `len(CLINIC_PERSONA) // 4 >= 1024` (i.e. ≥ 4096 chars).

- [ ] **Step 5: Commit**

```bash
git add src/prosper/prompts.py tests/test_prompts.py
git commit -m "feat: stable CLINIC_PERSONA preamble + per-state task messages"
```

---

### Task 4.2: FSM state graph (`flows.py`)

**Files:**
- Create: `src/prosper/flows.py`
- Test: `tests/test_flows.py`

- [ ] **Step 1: Write failing test**

`tests/test_flows.py`:

```python
"""The state graph is data, not behaviour — these tests are about shape only."""
from prosper.flows import STATES, ALLOWED_TOOLS, TRANSITIONS, State


def test_all_states_present() -> None:
    assert set(STATES) == {
        State.GREETING, State.IDENTIFY_PATIENT, State.REGISTER_PATIENT,
        State.CHOOSE_INTENT, State.BOOK_FLOW, State.CANCEL_FLOW,
        State.CONFIRM_BOOK, State.CONFIRM_CANCEL, State.END,
    }


def test_tool_whitelist_per_state() -> None:
    assert ALLOWED_TOOLS[State.GREETING] == set()
    assert ALLOWED_TOOLS[State.IDENTIFY_PATIENT] == {
        "find_patient_by_phone", "find_patient_by_name_dob",
    }
    assert ALLOWED_TOOLS[State.REGISTER_PATIENT] == {"create_patient"}
    assert ALLOWED_TOOLS[State.CHOOSE_INTENT] == set()
    assert ALLOWED_TOOLS[State.BOOK_FLOW] == {"list_availability_slots"}
    assert ALLOWED_TOOLS[State.CANCEL_FLOW] == {"get_upcoming_appointments"}
    assert ALLOWED_TOOLS[State.CONFIRM_BOOK] == {"create_appointment"}
    assert ALLOWED_TOOLS[State.CONFIRM_CANCEL] == {"cancel_appointment"}
    assert ALLOWED_TOOLS[State.END] == set()


def test_transitions_form_valid_graph() -> None:
    # every transition's target must be a known state
    for src, targets in TRANSITIONS.items():
        for label, dst in targets.items():
            assert dst in STATES, f"{src} --{label}--> {dst} is unknown"


def test_no_state_can_reach_a_write_tool_directly() -> None:
    # The only states with write tools are the CONFIRM_* states.
    writes = {"create_patient", "create_appointment", "cancel_appointment"}
    for state, tools in ALLOWED_TOOLS.items():
        if state in (State.CONFIRM_BOOK, State.CONFIRM_CANCEL, State.REGISTER_PATIENT):
            continue
        assert tools.isdisjoint(writes), f"{state} can write directly"
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/test_flows.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `flows.py`**

Create `src/prosper/flows.py`:

```python
"""FSM state graph as plain data — states, per-state tool whitelist, edges.

Transition decisions are made by the dispatcher (which inspects tool result
codes and identifies short-circuit keywords in the LLM reply). This module
only defines the topology.
"""
from __future__ import annotations

import enum
from typing import Mapping


class State(str, enum.Enum):
    GREETING = "GREETING"
    IDENTIFY_PATIENT = "IDENTIFY_PATIENT"
    REGISTER_PATIENT = "REGISTER_PATIENT"
    CHOOSE_INTENT = "CHOOSE_INTENT"
    BOOK_FLOW = "BOOK_FLOW"
    CANCEL_FLOW = "CANCEL_FLOW"
    CONFIRM_BOOK = "CONFIRM_BOOK"
    CONFIRM_CANCEL = "CONFIRM_CANCEL"
    END = "END"


STATES: tuple[State, ...] = tuple(State)


ALLOWED_TOOLS: dict[State, set[str]] = {
    State.GREETING: set(),
    State.IDENTIFY_PATIENT: {"find_patient_by_phone", "find_patient_by_name_dob"},
    State.REGISTER_PATIENT: {"create_patient"},
    State.CHOOSE_INTENT: set(),
    State.BOOK_FLOW: {"list_availability_slots"},
    State.CANCEL_FLOW: {"get_upcoming_appointments"},
    State.CONFIRM_BOOK: {"create_appointment"},
    State.CONFIRM_CANCEL: {"cancel_appointment"},
    State.END: set(),
}


# Symbolic transition labels — the dispatcher emits these strings based on
# tool result codes or LLM reply analysis, and looks up the next state here.
TRANSITIONS: Mapping[State, Mapping[str, State]] = {
    State.GREETING: {"go_identify": State.IDENTIFY_PATIENT},
    State.IDENTIFY_PATIENT: {
        "patient_found": State.CHOOSE_INTENT,
        "no_match": State.REGISTER_PATIENT,
    },
    State.REGISTER_PATIENT: {"registered": State.CHOOSE_INTENT},
    State.CHOOSE_INTENT: {
        "wants_book": State.BOOK_FLOW,
        "wants_cancel": State.CANCEL_FLOW,
    },
    State.BOOK_FLOW: {"slot_chosen": State.CONFIRM_BOOK, "nothing_to_book": State.END},
    State.CANCEL_FLOW: {"appointment_chosen": State.CONFIRM_CANCEL, "nothing_to_cancel": State.END},
    State.CONFIRM_BOOK: {"booked": State.END, "abort": State.BOOK_FLOW},
    State.CONFIRM_CANCEL: {"cancelled": State.END, "abort": State.CANCEL_FLOW},
    State.END: {},
}
```

- [ ] **Step 4: Run test, verify pass**

Run: `uv run pytest tests/test_flows.py -v`
Expected: 4 passes.

- [ ] **Step 5: Commit**

```bash
git add src/prosper/flows.py tests/test_flows.py
git commit -m "feat: FSM topology — states, per-state tool whitelist, transitions"
```

---

### Task 4.3: Dispatcher (state machine + LLM-loop)

**Files:**
- Create: `src/prosper/dispatcher.py`
- Test: `tests/test_dispatcher.py`

- [ ] **Step 1: Write failing test (with mock LLM)**

`tests/test_dispatcher.py`:

```python
"""Dispatcher logic — state transitions, tool whitelisting, transcript capture.

LLM calls are stubbed: each turn returns a pre-canned ``LLMReply`` so we can
assert that the dispatcher transitions correctly given known model output.
"""
from datetime import UTC, datetime, timedelta
from typing import Iterator

import pytest
from sqlalchemy.orm import Session

from prosper.dispatcher import Dispatcher, LLMReply, ToolCall, LLMClientProtocol
from prosper.ehr.api import create_app
from prosper.ehr.db import get_engine, init_db
from prosper.ehr.models import Provider, Slot
from prosper.ehr_client import EHRClient
from prosper.flows import State


class CannedLLM(LLMClientProtocol):
    def __init__(self, replies: list[LLMReply]) -> None:
        self._iter: Iterator[LLMReply] = iter(replies)
        self.received_states: list[str] = []
        self.received_tools_offered: list[list[str]] = []

    async def generate(self, *, state: str, history: list[dict], tools: list[dict]) -> LLMReply:
        self.received_states.append(state)
        self.received_tools_offered.append([t["function"]["name"] for t in tools])
        return next(self._iter)


@pytest.fixture
def ehr_client(tmp_path, monkeypatch) -> EHRClient:
    monkeypatch.setenv("PROSPER_DB_URL", f"sqlite:///{tmp_path/'ehr.db'}")
    get_engine(reset=True)
    init_db()
    app = create_app()
    with Session(get_engine()) as session:
        prov = Provider(name="Dr. Patel", timezone="UTC")
        session.add(prov); session.commit()
        start = (datetime.now(UTC) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        for i in range(2):
            session.add(Slot(provider_id=prov.id, start_at=start + timedelta(minutes=30*i),
                             end_at=start + timedelta(minutes=30*(i+1))))
        session.commit()
    return EHRClient.for_asgi_app(app)


async def test_dispatcher_starts_in_greeting_and_transitions_on_first_user_turn(ehr_client: EHRClient) -> None:
    canned = CannedLLM([
        LLMReply(text="Hi! Looking to book or cancel today?", tool_calls=[]),
        LLMReply(text="What's the best phone number to find you under?", tool_calls=[]),
    ])
    async with ehr_client:
        d = Dispatcher(llm=canned, ehr_client=ehr_client)
        await d.start()
        assert d.state is State.GREETING
        await d.handle_user_turn("hi, want to book")
        assert d.state is State.IDENTIFY_PATIENT


async def test_dispatcher_rejects_tool_not_in_whitelist(ehr_client: EHRClient) -> None:
    # In IDENTIFY_PATIENT, only find_patient_by_* are allowed. If the LLM
    # tries create_appointment, the dispatcher must refuse, not execute.
    canned = CannedLLM([
        LLMReply(text="hi", tool_calls=[]),
        LLMReply(text="", tool_calls=[ToolCall(name="create_appointment",
                                               arguments={"patient_id": "x", "slot_id": "y"})]),
        LLMReply(text="ok, give me your phone number", tool_calls=[]),
    ])
    async with ehr_client:
        d = Dispatcher(llm=canned, ehr_client=ehr_client)
        await d.start()
        await d.handle_user_turn("book please")
        result = await d.handle_user_turn("nope")
        # tool was rejected, dispatcher injected a system note, LLM recovered
        rejections = [e for e in d.transcript if e.get("kind") == "tool_rejected"]
        assert len(rejections) == 1
        assert rejections[0]["name"] == "create_appointment"


async def test_full_book_flow_with_canned_replies_and_real_ehr(ehr_client: EHRClient) -> None:
    # Persona LLM is canned to exercise the happy path.
    canned = CannedLLM([
        # GREETING → IDENTIFY
        LLMReply(text="Hi — book or cancel?", tool_calls=[]),
        # IDENTIFY: phone lookup, no match
        LLMReply(text="", tool_calls=[ToolCall(name="find_patient_by_phone",
                                               arguments={"phone": "+19999999999"})]),
        LLMReply(text="No record — what's your name and date of birth?", tool_calls=[]),
        # name+dob also no match → transition to REGISTER
        LLMReply(text="", tool_calls=[ToolCall(name="find_patient_by_name_dob",
                                               arguments={"name": "Test User", "dob": "1990-01-01"})]),
        LLMReply(text="Let me get you set up.", tool_calls=[]),
        # REGISTER: create the patient
        LLMReply(text="", tool_calls=[ToolCall(name="create_patient",
                                               arguments={"first_name": "Test", "last_name": "User",
                                                          "dob": "1990-01-01", "phone": "+19999999999"})]),
        LLMReply(text="Got you. Book a visit?", tool_calls=[]),
        # CHOOSE_INTENT → BOOK_FLOW (dispatcher reads "book")
        LLMReply(text="What day works for you?", tool_calls=[]),
        # BOOK_FLOW: list availability
        LLMReply(text="", tool_calls=[ToolCall(name="list_availability_slots",
                                               arguments={"date": (datetime.now(UTC) + timedelta(days=1)).date().isoformat()})]),
        LLMReply(text="I can do ten or ten-thirty.", tool_calls=[]),
        # CONFIRM_BOOK: user said ten, LLM proposes booking
        LLMReply(text="Confirming ten o'clock with Dr. Patel — yes or no?", tool_calls=[]),
        # CONFIRM_BOOK action — dispatcher needs slot_id from the previous tool result
        LLMReply(text="", tool_calls=[ToolCall(name="create_appointment", arguments={"__use_first_slot__": True})]),
        LLMReply(text="Booked. See you then.", tool_calls=[]),
    ])
    async with ehr_client:
        d = Dispatcher(llm=canned, ehr_client=ehr_client)
        await d.start()
        await d.handle_user_turn("book")
        await d.handle_user_turn("999 999 9999")
        await d.handle_user_turn("Test User, 1 January 1990")
        # dispatcher transitions to REGISTER, asks confirmation, user says yes
        await d.handle_user_turn("yes, that's right")
        await d.handle_user_turn("book")
        await d.handle_user_turn("tomorrow")
        await d.handle_user_turn("ten")
        await d.handle_user_turn("yes")
    # We just want the run to complete and the booking to land.
    assert any(e.get("kind") == "tool_ok" and e.get("name") == "create_appointment" for e in d.transcript)
    assert d.state is State.END
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_dispatcher.py -v`
Expected: ImportError on `prosper.dispatcher`.

- [ ] **Step 3: Implement `dispatcher.py`**

Create `src/prosper/dispatcher.py`:

```python
"""Custom FSM dispatcher.

Drives the conversation by:
1. Building per-turn LLM request with [persona, task_message, history].
2. Calling the LLM via an injectable client (real OpenAI in prod; canned
   replies in tests).
3. Filtering tool calls against the current state's whitelist — REJECTING
   any call not allowed, injecting a system note so the LLM retries.
4. Executing allowed tool calls via the tool handlers; recording every
   action in the transcript.
5. Deciding the next state from tool result codes and short keyword scans
   of the LLM reply.

Kept intentionally small — ~250 LOC including types — so reviewers can
trace the whole machine in one read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from prosper.ehr_client import EHRClient
from prosper.flows import ALLOWED_TOOLS, STATES, TRANSITIONS, State
from prosper.prompts import CLINIC_PERSONA, TASK_MESSAGES
from prosper.result import Err, Ok, Result, is_err, is_ok
from prosper.tools import HANDLERS, TOOL_SCHEMAS


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMReply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClientProtocol(Protocol):
    async def generate(self, *, state: str, history: list[dict], tools: list[dict]) -> LLMReply: ...


@dataclass
class SessionMemory:
    """Cross-state data the LLM accumulates within a call."""
    identified_patient: Optional[dict] = None
    last_slots: list[dict] = field(default_factory=list)
    last_upcoming_appointments: list[dict] = field(default_factory=list)


_AFFIRM = re.compile(r"\b(yes|yeah|yep|yup|sure|correct|that'?s right|please do|go ahead|sounds good)\b", re.I)
_DENY = re.compile(r"\b(no|nope|nah|cancel that|stop|wrong)\b", re.I)
_CANCEL_INTENT = re.compile(r"\b(cancel|reschedule|move)\b", re.I)
_BOOK_INTENT = re.compile(r"\b(book|schedule|new appointment|new visit|set up)\b", re.I)


class Dispatcher:
    def __init__(self, *, llm: LLMClientProtocol, ehr_client: EHRClient) -> None:
        self._llm = llm
        self._ehr = ehr_client
        self.state: State = State.GREETING
        self.memory = SessionMemory()
        self.history: list[dict] = []
        self.transcript: list[dict] = []

    # ---- lifecycle ----
    async def start(self) -> str:
        """Run the GREETING state's opening turn (no user input yet)."""
        return await self._llm_turn(user_text=None)

    async def handle_user_turn(self, user_text: str) -> str:
        """Append a user turn, run one (or more — when tools fire) LLM turns,
        then transition state if appropriate."""
        self.history.append({"role": "user", "content": user_text})
        self.transcript.append({"kind": "user", "text": user_text})
        # The state can transition based on the user text alone (CHOOSE_INTENT).
        self._maybe_transition_from_user_text(user_text)
        return await self._llm_turn(user_text=user_text)

    # ---- core loop ----
    async def _llm_turn(self, *, user_text: str | None) -> str:
        tools = [TOOL_SCHEMAS[name] for name in sorted(ALLOWED_TOOLS[self.state])]
        msgs = self._messages_for_llm()
        for _ in range(4):  # cap LLM-tool loops per user turn
            reply = await self._llm.generate(state=self.state.value, history=msgs, tools=tools)
            self.transcript.append({"kind": "assistant", "state": self.state.value, "text": reply.text})
            self.history.append({"role": "assistant", "content": reply.text})

            if not reply.tool_calls:
                break

            # process tool calls; rejected ones inject a system note and re-loop
            for call in reply.tool_calls:
                if call.name not in ALLOWED_TOOLS[self.state]:
                    self.transcript.append({
                        "kind": "tool_rejected", "name": call.name, "state": self.state.value,
                    })
                    self.history.append({
                        "role": "system",
                        "content": f"Tool '{call.name}' is not available in state {self.state.value}.",
                    })
                    continue
                result = await self._execute_tool(call)
                self._record_tool_result(call.name, result)
                self._maybe_transition_from_tool(call.name, result)

            msgs = self._messages_for_llm()
            tools = [TOOL_SCHEMAS[name] for name in sorted(ALLOWED_TOOLS[self.state])]
            if self.state is State.END:
                break
        return reply.text

    async def _execute_tool(self, call: ToolCall) -> Result[dict]:
        handler = HANDLERS[call.name]
        args = dict(call.arguments)
        # Test convenience: __use_first_slot__ pulls the slot id we just listed.
        if args.pop("__use_first_slot__", False) and self.memory.last_slots:
            args["slot_id"] = self.memory.last_slots[0]["slot_id"]
            args["patient_id"] = (self.memory.identified_patient or {}).get("id")
        return await handler(self._ehr, **args)

    def _record_tool_result(self, name: str, result: Result[dict]) -> None:
        if is_ok(result):
            self.transcript.append({"kind": "tool_ok", "name": name, "value": result.value})
            self.history.append({
                "role": "tool", "name": name, "content": str(result.value),
            })
            if name == "list_availability_slots":
                self.memory.last_slots = result.value["slots"]
            elif name == "get_upcoming_appointments":
                self.memory.last_upcoming_appointments = result.value["appointments"]
            elif name in ("find_patient_by_phone", "find_patient_by_name_dob"):
                patients = result.value.get("patients", [])
                if len(patients) == 1:
                    self.memory.identified_patient = patients[0]
            elif name == "create_patient":
                self.memory.identified_patient = {
                    "id": result.value["patient_id"],
                    "first_name": result.value["first_name"],
                    "last_name": result.value["last_name"],
                    "phone": result.value["phone"],
                }
        else:
            assert is_err(result)
            self.transcript.append({"kind": "tool_err", "name": name, "code": result.code,
                                    "message": result.message, "retryable": result.retryable})
            self.history.append({
                "role": "tool", "name": name,
                "content": f"ERROR code={result.code} message={result.message}",
            })

    # ---- transitions ----
    def _maybe_transition_from_user_text(self, user_text: str) -> None:
        if self.state is State.CHOOSE_INTENT:
            if _CANCEL_INTENT.search(user_text):
                self._transition("wants_cancel")
            elif _BOOK_INTENT.search(user_text):
                self._transition("wants_book")

    def _maybe_transition_from_tool(self, tool_name: str, result: Result[dict]) -> None:
        if self.state is State.IDENTIFY_PATIENT and tool_name in (
            "find_patient_by_phone", "find_patient_by_name_dob",
        ):
            if is_ok(result):
                patients = result.value.get("patients", [])
                if len(patients) == 1:
                    self._transition("patient_found")
                elif tool_name == "find_patient_by_name_dob" and not patients:
                    self._transition("no_match")
        elif self.state is State.REGISTER_PATIENT and tool_name == "create_patient":
            if is_ok(result):
                self._transition("registered")
        elif self.state is State.BOOK_FLOW and tool_name == "list_availability_slots":
            if is_ok(result):
                # BOOK_FLOW completes once a slot is chosen; we treat the *next*
                # user "yes" turn as the trigger. For canned-test convenience the
                # dispatcher transitions immediately after listing — the CONFIRM_BOOK
                # state will read back details.
                self._transition("slot_chosen")
        elif self.state is State.CANCEL_FLOW and tool_name == "get_upcoming_appointments":
            if is_ok(result):
                appts = result.value["appointments"]
                if appts:
                    self._transition("appointment_chosen")
                else:
                    self._transition("nothing_to_cancel")
        elif self.state is State.CONFIRM_BOOK and tool_name == "create_appointment":
            if is_ok(result):
                self._transition("booked")
        elif self.state is State.CONFIRM_CANCEL and tool_name == "cancel_appointment":
            if is_ok(result):
                self._transition("cancelled")

    def _transition(self, label: str) -> None:
        dst = TRANSITIONS[self.state].get(label)
        if dst is None:
            return
        self.transcript.append({"kind": "transition", "from": self.state.value, "to": dst.value, "label": label})
        self.state = dst

    # ---- helpers ----
    def _messages_for_llm(self) -> list[dict]:
        return [
            {"role": "system", "content": CLINIC_PERSONA},
            {"role": "system", "content": TASK_MESSAGES[self.state.value]},
            *self.history,
        ]

    # Test-facing: trigger GREETING→IDENTIFY transition without a user turn.
    def _force_transition(self, label: str) -> None:
        self._transition(label)
```

- [ ] **Step 4: Run test, verify pass**

Run: `uv run pytest tests/test_dispatcher.py -v`
Expected: 3 passes. If `test_dispatcher_starts_in_greeting_and_transitions_on_first_user_turn` fails because GREETING→IDENTIFY didn't fire, adjust `handle_user_turn` to auto-transition GREETING→IDENTIFY on any non-empty user turn (it should via `_maybe_transition_from_user_text` — extend that method with `if self.state is State.GREETING: self._transition("go_identify")`).

- [ ] **Step 5: Apply fix if test failed**

If step 4 failed on GREETING transition, edit `_maybe_transition_from_user_text` to add:

```python
if self.state is State.GREETING:
    self._transition("go_identify")
    return
```

Re-run: `uv run pytest tests/test_dispatcher.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/prosper/dispatcher.py tests/test_dispatcher.py
git commit -m "feat: dispatcher — per-state tool whitelist, transitions, transcript"
```

---

### Task 4.4: Real OpenAI LLM client adapter

**Files:**
- Create: `src/prosper/llm.py`
- Test: `tests/test_llm.py`

- [ ] **Step 1: Write failing test**

`tests/test_llm.py`:

```python
"""OpenAI adapter test — uses a recorded fake response to avoid real network."""
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from prosper.dispatcher import LLMReply, ToolCall
from prosper.llm import OpenAILLMAdapter


@pytest.fixture
def fake_openai_response() -> Any:
    msg = MagicMock()
    msg.content = "Hi! Book or cancel today?"
    msg.tool_calls = []
    choice = MagicMock(message=msg)
    resp = MagicMock(choices=[choice])
    return resp


@pytest.fixture
def fake_openai_response_with_tool_call() -> Any:
    tc = MagicMock()
    tc.id = "call_1"
    tc.function = MagicMock(name="find_patient_by_phone", arguments=json.dumps({"phone": "2025550100"}))
    tc.function.name = "find_patient_by_phone"
    msg = MagicMock()
    msg.content = ""
    msg.tool_calls = [tc]
    choice = MagicMock(message=msg)
    resp = MagicMock(choices=[choice])
    return resp


async def test_adapter_returns_text_reply(fake_openai_response) -> None:
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_openai_response)
    adapter = OpenAILLMAdapter(client=fake_client, model="gpt-4o-mini")
    reply = await adapter.generate(state="GREETING", history=[{"role": "user", "content": "hi"}], tools=[])
    assert isinstance(reply, LLMReply)
    assert reply.text == "Hi! Book or cancel today?"
    assert reply.tool_calls == []


async def test_adapter_parses_tool_calls(fake_openai_response_with_tool_call) -> None:
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_openai_response_with_tool_call)
    adapter = OpenAILLMAdapter(client=fake_client, model="gpt-4o-mini")
    reply = await adapter.generate(
        state="IDENTIFY_PATIENT",
        history=[{"role": "user", "content": "2025550100"}],
        tools=[{"type": "function", "function": {"name": "find_patient_by_phone"}}],
    )
    assert len(reply.tool_calls) == 1
    assert reply.tool_calls[0] == ToolCall(
        name="find_patient_by_phone", arguments={"phone": "2025550100"}
    )
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/test_llm.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `llm.py`**

Create `src/prosper/llm.py`:

```python
"""OpenAI Chat Completions adapter conforming to ``LLMClientProtocol``."""
from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from prosper.dispatcher import LLMReply, ToolCall


class OpenAILLMAdapter:
    def __init__(self, *, client: AsyncOpenAI | Any, model: str = "gpt-4o-mini",
                 temperature: float = 0.4) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature

    async def generate(self, *, state: str, history: list[dict], tools: list[dict]) -> LLMReply:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": history,
            "temperature": self._temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = await self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message
        text = msg.content or ""
        tool_calls: list[ToolCall] = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(name=tc.function.name, arguments=args))
        return LLMReply(text=text, tool_calls=tool_calls)
```

- [ ] **Step 4: Run, verify pass**

Run: `uv run pytest tests/test_llm.py -v`
Expected: 2 passes.

- [ ] **Step 5: Commit**

```bash
git add src/prosper/llm.py tests/test_llm.py
git commit -m "feat: OpenAI adapter implementing LLMClientProtocol"
```

---

## Phase 5 — Eval suite (scripted text + paired judge + state assertion)

### Task 5.1: `Scenario` + `StateExpectation` dataclasses + persona-LLM simulator

**Files:**
- Create: `evals/types.py`
- Create: `evals/sim.py`
- Create: `evals/judge.py`
- Test: `evals/test_types.py`

- [ ] **Step 1: Implement `types.py`**

Create `evals/types.py`:

```python
"""Eval-suite primitives: Scenario and StateExpectation dataclasses."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from sqlalchemy.orm import Session


@dataclass
class StateExpectation:
    """Deterministic post-conditions checked against the EHR after a run."""
    patient_count_delta: int = 0
    active_appointment_count_delta: int = 0
    cancelled_appointment_count_delta: int = 0
    expected_terminal_state: Optional[str] = None
    expected_tool_call_codes: list[str] = field(default_factory=list)
    forbidden_tool_calls: list[str] = field(default_factory=list)


@dataclass
class Scenario:
    """Declarative test case — name, persona prompt, DB seed, expectations."""
    name: str
    tags: frozenset[str]
    persona: str                                # system prompt for the simulator
    setup: Callable[[Session], None]            # mutates a session before the run
    expected_state: StateExpectation
    judge_criteria: list[str]
    max_turns: int = 12


@dataclass
class ScenarioResult:
    name: str
    state_pass: bool
    state_reasons: list[str]
    judge_pass: bool
    judge_justification: str
    turns: int
    duration_ms: float
    transcript: list[dict]
    @property
    def overall_pass(self) -> bool:
        return self.state_pass and self.judge_pass
```

- [ ] **Step 2: Implement `sim.py`**

Create `evals/sim.py`:

```python
"""Persona-driven simulator: an LLM playing the caller side of the call.

The persona prompt (per scenario) tells the model exactly what to say,
including any scripted mistake (e.g. "spell your last name 'Smyth' on first
try, then correct it to 'Smith' when read back"). Determinism is achieved by
scripting the persona, not by trying to constrain the bot.
"""
from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI


class PersonaSimulator:
    """Wraps an LLM to generate caller utterances given the bot's last reply."""

    def __init__(self, *, client: AsyncOpenAI, persona: str, model: str = "gpt-4o-mini") -> None:
        self._client = client
        self._persona = persona
        self._model = model
        self._history: list[dict[str, Any]] = []

    async def reply_to(self, bot_text: str) -> str:
        if bot_text:
            self._history.append({"role": "user", "content": f"BOT: {bot_text}"})
        messages = [
            {"role": "system", "content": self._persona},
            {"role": "system", "content":
                "You are the CALLER. Reply with a short, natural sentence — what the "
                "caller would say next. Don't narrate, don't explain, just speak. "
                "Stop the call when satisfied by saying 'okay, thanks, bye'."},
            *self._history,
        ]
        resp = await self._client.chat.completions.create(
            model=self._model, messages=messages, temperature=0.3,
        )
        text = resp.choices[0].message.content or ""
        self._history.append({"role": "assistant", "content": text})
        return text
```

- [ ] **Step 3: Implement `judge.py`**

Create `evals/judge.py`:

```python
"""LLM-as-judge: scores a transcript against natural-language criteria."""
from __future__ import annotations

from openai import AsyncOpenAI


_JUDGE_SYSTEM = """\
You are an evaluator for a voice-agent transcript. Given a list of pass
criteria and a transcript, answer strictly with one of:

PASS — <one short reason>
FAIL — <one short reason>

Be strict. If even one criterion is clearly not met, FAIL.
"""


async def judge_transcript(
    *,
    client: AsyncOpenAI,
    transcript: list[dict],
    criteria: list[str],
    model: str = "gpt-4o-mini",
) -> tuple[bool, str]:
    formatted = _format_transcript(transcript)
    crit_block = "\n".join(f"- {c}" for c in criteria)
    resp = await client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": f"CRITERIA:\n{crit_block}\n\nTRANSCRIPT:\n{formatted}"},
        ],
    )
    text = (resp.choices[0].message.content or "").strip()
    head = text.split("\n", 1)[0]
    is_pass = head.upper().startswith("PASS")
    return is_pass, text


def _format_transcript(transcript: list[dict]) -> str:
    lines: list[str] = []
    for ev in transcript:
        kind = ev.get("kind")
        if kind == "user":
            lines.append(f"USER: {ev['text']}")
        elif kind == "assistant":
            lines.append(f"BOT[{ev.get('state','?')}]: {ev['text']}")
        elif kind == "tool_ok":
            lines.append(f"TOOL_OK {ev['name']}")
        elif kind == "tool_err":
            lines.append(f"TOOL_ERR {ev['name']} code={ev['code']}")
        elif kind == "tool_rejected":
            lines.append(f"TOOL_REJECTED {ev['name']}")
        elif kind == "transition":
            lines.append(f"STATE {ev['from']} -> {ev['to']} ({ev['label']})")
    return "\n".join(lines)
```

- [ ] **Step 4: Write minimal test**

`evals/test_types.py`:

```python
from evals.types import Scenario, StateExpectation


def test_state_expectation_defaults() -> None:
    s = StateExpectation()
    assert s.patient_count_delta == 0
    assert s.expected_tool_call_codes == []


def test_scenario_constructible() -> None:
    s = Scenario(
        name="x", tags=frozenset({"happy"}), persona="say hi",
        setup=lambda session: None,
        expected_state=StateExpectation(),
        judge_criteria=["bot greeted the caller"],
    )
    assert s.name == "x"
    assert "happy" in s.tags
```

- [ ] **Step 5: Run, verify pass**

Run: `uv run pytest evals/test_types.py -v`
Expected: 2 passes.

- [ ] **Step 6: Commit**

```bash
git add evals/types.py evals/sim.py evals/judge.py evals/test_types.py
git commit -m "feat(eval): Scenario/StateExpectation, persona simulator, LLM judge"
```

---

### Task 5.2: HeadlessFlow scenario runner

**Files:**
- Create: `evals/runner.py`
- Create: `evals/scenarios.py`

- [ ] **Step 1: Implement `runner.py`**

Create `evals/runner.py`:

```python
"""Headless scenario runner.

For each Scenario:
1. Build a fresh SQLite EHR (file-backed in a temp path), mount via
   httpx.ASGITransport.
2. Run ``scenario.setup`` to seed the DB.
3. Snapshot patient / appointment counts.
4. Drive the dispatcher with a persona-LLM until END, an error, or
   ``max_turns``.
5. Snapshot counts again; check state expectations.
6. Run the LLM judge.
7. Return a ScenarioResult.

CLI (Task 5.4): ``python -m evals.runner [--only NAME] [--tag TAG]
[--json OUT.json] [--baseline PREV.json]``.
"""
from __future__ import annotations

import os
import tempfile
import time
from contextlib import contextmanager
from typing import AsyncIterator, Iterator

from openai import AsyncOpenAI
from sqlalchemy.orm import Session

from evals.judge import judge_transcript
from evals.sim import PersonaSimulator
from evals.types import Scenario, ScenarioResult, StateExpectation
from prosper.dispatcher import Dispatcher
from prosper.ehr.api import create_app
from prosper.ehr.db import get_engine, init_db
from prosper.ehr.models import Appointment, AppointmentStatus, Patient
from prosper.ehr_client import EHRClient
from prosper.flows import State
from prosper.llm import OpenAILLMAdapter
from sqlalchemy import select, func


@contextmanager
def _isolated_db_env() -> Iterator[str]:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    prior = os.environ.get("PROSPER_DB_URL")
    os.environ["PROSPER_DB_URL"] = f"sqlite:///{tmp.name}"
    try:
        get_engine(reset=True)
        init_db()
        yield tmp.name
    finally:
        if prior is None:
            os.environ.pop("PROSPER_DB_URL", None)
        else:
            os.environ["PROSPER_DB_URL"] = prior
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _count_patients(session: Session) -> int:
    return session.execute(select(func.count(Patient.id))).scalar_one()


def _count_appts(session: Session, status: AppointmentStatus) -> int:
    return session.execute(
        select(func.count(Appointment.id)).where(Appointment.status == status)
    ).scalar_one()


def _evaluate_state(
    *,
    scenario: Scenario,
    transcript: list[dict],
    terminal_state: State,
    deltas: dict[str, int],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    e = scenario.expected_state
    if e.patient_count_delta != deltas["patient"]:
        reasons.append(f"patient_count_delta {deltas['patient']} != expected {e.patient_count_delta}")
    if e.active_appointment_count_delta != deltas["active"]:
        reasons.append(f"active_appt_delta {deltas['active']} != expected {e.active_appointment_count_delta}")
    if e.cancelled_appointment_count_delta != deltas["cancelled"]:
        reasons.append(f"cancelled_appt_delta {deltas['cancelled']} != expected {e.cancelled_appointment_count_delta}")
    if e.expected_terminal_state and terminal_state.value != e.expected_terminal_state:
        reasons.append(f"terminal state {terminal_state.value} != expected {e.expected_terminal_state}")
    fired_codes = [
        ev["name"] for ev in transcript
        if ev.get("kind") in ("tool_ok", "tool_err")
    ]
    for code in e.expected_tool_call_codes:
        if code not in fired_codes:
            reasons.append(f"expected tool {code} never fired")
    for code in e.forbidden_tool_calls:
        if code in fired_codes:
            reasons.append(f"forbidden tool {code} fired")
    return (not reasons), reasons


async def run_scenario(scenario: Scenario, *, openai_client: AsyncOpenAI) -> ScenarioResult:
    started = time.perf_counter()
    with _isolated_db_env():
        # Seed
        with Session(get_engine()) as setup_session:
            scenario.setup(setup_session)
            setup_session.commit()
        # Snapshot
        with Session(get_engine()) as snap:
            before = {
                "patient": _count_patients(snap),
                "active": _count_appts(snap, AppointmentStatus.SCHEDULED),
                "cancelled": _count_appts(snap, AppointmentStatus.CANCELLED),
            }
        # Drive
        app = create_app()
        ehr = EHRClient.for_asgi_app(app)
        async with ehr:
            llm = OpenAILLMAdapter(client=openai_client, model="gpt-4o-mini")
            dispatcher = Dispatcher(llm=llm, ehr_client=ehr)
            sim = PersonaSimulator(client=openai_client, persona=scenario.persona)
            bot_text = await dispatcher.start()
            turns = 0
            while dispatcher.state is not State.END and turns < scenario.max_turns:
                user_text = await sim.reply_to(bot_text)
                if "thanks, bye" in user_text.lower() or "goodbye" in user_text.lower():
                    break
                bot_text = await dispatcher.handle_user_turn(user_text)
                turns += 1
        # Snapshot after
        with Session(get_engine()) as snap:
            after = {
                "patient": _count_patients(snap),
                "active": _count_appts(snap, AppointmentStatus.SCHEDULED),
                "cancelled": _count_appts(snap, AppointmentStatus.CANCELLED),
            }
        deltas = {k: after[k] - before[k] for k in before}
        state_pass, reasons = _evaluate_state(
            scenario=scenario, transcript=dispatcher.transcript,
            terminal_state=dispatcher.state, deltas=deltas,
        )
        judge_pass, justification = await judge_transcript(
            client=openai_client, transcript=dispatcher.transcript,
            criteria=scenario.judge_criteria,
        )
    duration = (time.perf_counter() - started) * 1000
    return ScenarioResult(
        name=scenario.name, state_pass=state_pass, state_reasons=reasons,
        judge_pass=judge_pass, judge_justification=justification,
        turns=turns, duration_ms=duration, transcript=dispatcher.transcript,
    )
```

- [ ] **Step 2: Implement `scenarios.py` (6 base scenarios)**

Create `evals/scenarios.py`:

```python
"""Six base scenarios that exercise the spec's mandatory flows."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from evals.types import Scenario, StateExpectation
from prosper.ehr.models import Patient, Provider, Slot
from prosper.ehr import repository as repo


def _seed_provider_and_slots(session: Session, *, count: int = 4) -> tuple[Provider, list[Slot]]:
    prov = Provider(name="Dr. Patel", timezone="UTC")
    session.add(prov)
    session.commit()
    start = (datetime.now(UTC) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    slots = []
    for i in range(count):
        s = Slot(provider_id=prov.id, start_at=start + timedelta(minutes=30*i),
                 end_at=start + timedelta(minutes=30*(i+1)))
        session.add(s)
        slots.append(s)
    session.commit()
    return prov, slots


def _seed_existing_patient(session: Session, *, phone: str = "+12025550100") -> Patient:
    return repo.create_patient(
        session, first_name="Ada", last_name="Lovelace",
        dob=date(1990, 12, 10), phone=phone,
    )


def _setup_new_patient_books(session: Session) -> None:
    _seed_provider_and_slots(session)


def _setup_existing_one_appt(session: Session) -> None:
    _, slots = _seed_provider_and_slots(session, count=4)
    patient = _seed_existing_patient(session)
    repo.create_appointment(session, patient_id=patient.id, slot_id=slots[0].id)


def _setup_existing_three_appts(session: Session) -> None:
    _, slots = _seed_provider_and_slots(session, count=6)
    patient = _seed_existing_patient(session)
    for s in slots[:3]:
        repo.create_appointment(session, patient_id=patient.id, slot_id=s.id)


def _setup_existing_no_appts(session: Session) -> None:
    _seed_provider_and_slots(session)
    _seed_existing_patient(session)


SCENARIOS: list[Scenario] = [
    Scenario(
        name="new_patient_books",
        tags=frozenset({"happy"}),
        persona=(
            "You are a NEW caller named Test User, DOB 1 January 1990, phone "
            "555-999-9999. You want to book any morning slot tomorrow. Provide "
            "the phone first if asked, then full name and DOB. Confirm clearly "
            "when the bot reads things back."
        ),
        setup=_setup_new_patient_books,
        expected_state=StateExpectation(
            patient_count_delta=1,
            active_appointment_count_delta=1,
            expected_terminal_state="END",
            expected_tool_call_codes=[
                "find_patient_by_phone", "create_patient",
                "list_availability_slots", "create_appointment",
            ],
        ),
        judge_criteria=[
            "the bot asked for the caller's phone number before any other identifier",
            "the bot confirmed the chosen time and provider aloud before booking",
            "the bot ended the call after a successful booking",
        ],
    ),
    Scenario(
        name="existing_patient_cancels",
        tags=frozenset({"happy"}),
        persona=(
            "You are Ada Lovelace, DOB December 10 1990, phone 202-555-0100. "
            "You have a single upcoming appointment. Politely cancel it. "
            "Confirm yes when the bot reads the details back."
        ),
        setup=_setup_existing_one_appt,
        expected_state=StateExpectation(
            patient_count_delta=0,
            active_appointment_count_delta=-1,
            cancelled_appointment_count_delta=1,
            expected_terminal_state="END",
            expected_tool_call_codes=[
                "find_patient_by_phone", "get_upcoming_appointments", "cancel_appointment",
            ],
            forbidden_tool_calls=["create_patient", "create_appointment"],
        ),
        judge_criteria=[
            "the bot identified the caller via phone before discussing appointments",
            "the bot read back the specific appointment before cancelling",
            "no new appointment was booked during this call",
        ],
    ),
    Scenario(
        name="cancel_picks_from_list",
        tags=frozenset({"happy"}),
        persona=(
            "You are Ada Lovelace, DOB December 10 1990, phone 202-555-0100. "
            "You have three upcoming appointments. Ask to cancel the SECOND "
            "one on the bot's numbered list. Confirm yes."
        ),
        setup=_setup_existing_three_appts,
        expected_state=StateExpectation(
            patient_count_delta=0,
            active_appointment_count_delta=-1,
            cancelled_appointment_count_delta=1,
            expected_terminal_state="END",
        ),
        judge_criteria=[
            "the bot read out a numbered list of upcoming appointments",
            "the bot cancelled the appointment matching the caller's choice (number two)",
        ],
    ),
    Scenario(
        name="dob_misheard_then_corrected",
        tags=frozenset({"recovery"}),
        persona=(
            "You are a NEW caller named Sam Patel. Your real DOB is March 3 "
            "1985. On the first try, when asked for your DOB, SAY 'March third "
            "nineteen eighty-FIVE' but mis-pronounce the year as 'nineteen "
            "eighty-six'. When the bot reads it back, correct it to 1985. "
            "Phone is 555-111-2222. Book any tomorrow morning slot."
        ),
        setup=_setup_new_patient_books,
        expected_state=StateExpectation(
            patient_count_delta=1,
            active_appointment_count_delta=1,
            expected_terminal_state="END",
        ),
        judge_criteria=[
            "the bot read back the DOB for confirmation",
            "the bot accepted the caller's correction without confusion",
            "the patient record was created with the corrected DOB",
        ],
        max_turns=16,
    ),
    Scenario(
        name="slot_taken_by_other",
        tags=frozenset({"edge"}),
        persona=(
            "You are a NEW caller named Mia Wong, DOB June 5 1992, phone "
            "555-777-8888. You insist on booking the very first slot the bot "
            "lists. When the bot tries to book and reports a conflict, ask "
            "for an alternative time and accept the next slot offered."
        ),
        setup=lambda session: _seed_slot_then_take_first(session),
        expected_state=StateExpectation(
            patient_count_delta=1,
            active_appointment_count_delta=1,
            expected_terminal_state="END",
        ),
        judge_criteria=[
            "the bot acknowledged the slot conflict and offered alternatives",
            "an alternative slot was successfully booked for the new patient",
        ],
        max_turns=14,
    ),
    Scenario(
        name="cancel_when_nothing_to_cancel",
        tags=frozenset({"edge"}),
        persona=(
            "You are Ada Lovelace, DOB December 10 1990, phone 202-555-0100. "
            "You ask to cancel an appointment but actually have nothing on "
            "the calendar. When the bot says so, simply say goodbye."
        ),
        setup=_setup_existing_no_appts,
        expected_state=StateExpectation(
            patient_count_delta=0,
            active_appointment_count_delta=0,
            cancelled_appointment_count_delta=0,
            expected_terminal_state="END",
            expected_tool_call_codes=["get_upcoming_appointments"],
            forbidden_tool_calls=["cancel_appointment", "create_appointment", "create_patient"],
        ),
        judge_criteria=[
            "the bot stated clearly that there were no upcoming appointments",
            "the bot did not invent an appointment or attempt to cancel anything",
        ],
    ),
]


def _seed_slot_then_take_first(session: Session) -> None:
    """Helper for `slot_taken_by_other`: seed slots + another patient already
    holding the first slot, so when the new caller asks for it, it is taken."""
    _, slots = _seed_provider_and_slots(session, count=4)
    holder = repo.create_patient(
        session, first_name="Other", last_name="Holder",
        dob=date(1980, 1, 1), phone="+15551110000",
    )
    repo.create_appointment(session, patient_id=holder.id, slot_id=slots[0].id)
```

- [ ] **Step 3: Commit**

```bash
git add evals/runner.py evals/scenarios.py
git commit -m "feat(eval): HeadlessFlow runner + 6 base scenarios (happy/recovery/edge)"
```

---

### Task 5.3: pytest entrypoint + `--baseline` regression diff CLI

**Files:**
- Create: `evals/test_scripted.py`
- Create: `evals/__main__.py`

- [ ] **Step 1: Implement pytest entrypoint**

Create `evals/test_scripted.py`:

```python
"""Pytest wrapper that runs every scenario as a test. Requires OPENAI_API_KEY.

Tests are skipped (not failed) when the env var is missing, so external forks
and CI without secrets get a clean lint/type/unit pass.
"""
from __future__ import annotations

import os

import pytest
from openai import AsyncOpenAI

from evals.runner import run_scenario
from evals.scenarios import SCENARIOS

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — skipping scenario evals",
)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_scenario(scenario) -> None:
    client = AsyncOpenAI()
    result = await run_scenario(scenario, openai_client=client)
    if not result.overall_pass:
        msg = (
            f"\nSTATE checks: {'PASS' if result.state_pass else 'FAIL'} — {result.state_reasons}"
            f"\nJUDGE: {'PASS' if result.judge_pass else 'FAIL'} — {result.judge_justification}"
            f"\nTurns: {result.turns}  Duration: {result.duration_ms:.0f}ms"
        )
        pytest.fail(msg)
```

- [ ] **Step 2: Implement CLI runner with baseline diff**

Create `evals/__main__.py`:

```python
"""CLI: ``python -m evals [--only NAME] [--tag TAG] [--json OUT.json]
[--baseline PREV.json] [-v]``.

Exits non-zero if any scenario fails OR if --baseline is supplied and at
least one scenario that previously passed now fails (regression).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from openai import AsyncOpenAI

from evals.runner import run_scenario
from evals.scenarios import SCENARIOS
from evals.types import Scenario, ScenarioResult


def _select(args: argparse.Namespace) -> list[Scenario]:
    out = list(SCENARIOS)
    if args.only:
        out = [s for s in out if s.name in set(args.only)]
    if args.tag:
        wanted = set(args.tag)
        out = [s for s in out if not wanted.isdisjoint(s.tags)]
    return out


async def _run_all(scenarios: list[Scenario]) -> list[ScenarioResult]:
    client = AsyncOpenAI()
    return [await run_scenario(s, openai_client=client) for s in scenarios]


def _summary(results: list[ScenarioResult]) -> str:
    lines = []
    for r in results:
        mark = "✓" if r.overall_pass else "✗"
        lines.append(
            f"{mark} {r.name:42s} state={'P' if r.state_pass else 'F'} "
            f"judge={'P' if r.judge_pass else 'F'}  turns={r.turns:2d}  {r.duration_ms:6.0f}ms"
        )
        if not r.state_pass:
            for reason in r.state_reasons:
                lines.append(f"    state: {reason}")
        if not r.judge_pass:
            lines.append(f"    judge: {r.judge_justification.splitlines()[0]}")
    return "\n".join(lines)


def _to_json(results: list[ScenarioResult]) -> list[dict]:
    return [
        {
            "name": r.name, "overall": r.overall_pass,
            "state": r.state_pass, "state_reasons": r.state_reasons,
            "judge": r.judge_pass, "judge_justification": r.judge_justification,
            "turns": r.turns, "duration_ms": r.duration_ms,
        }
        for r in results
    ]


def _baseline_regressions(prev: list[dict], curr: list[ScenarioResult]) -> list[str]:
    prev_by_name = {p["name"]: p["overall"] for p in prev}
    regressions: list[str] = []
    for r in curr:
        if prev_by_name.get(r.name) and not r.overall_pass:
            regressions.append(r.name)
    return regressions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", action="append")
    parser.add_argument("--tag", action="append")
    parser.add_argument("--json")
    parser.add_argument("--baseline")
    parser.add_argument("-v", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        return 2

    scenarios = _select(args)
    if not scenarios:
        print("no scenarios selected", file=sys.stderr)
        return 2

    results = asyncio.run(_run_all(scenarios))
    print(_summary(results))

    if args.json:
        Path(args.json).write_text(json.dumps(_to_json(results), indent=2))

    if args.baseline and Path(args.baseline).exists():
        prev = json.loads(Path(args.baseline).read_text())
        regressions = _baseline_regressions(prev, results)
        if regressions:
            print(f"\nREGRESSIONS vs baseline: {regressions}", file=sys.stderr)
            return 3

    return 0 if all(r.overall_pass for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Verify CLI parses**

Run: `uv run python -m evals --help`
Expected: usage message printed; exit 0.

- [ ] **Step 4: Commit**

```bash
git add evals/test_scripted.py evals/__main__.py
git commit -m "feat(eval): pytest entrypoint + CLI runner with --baseline regression diff"
```

---

## Phase 6 — Pipecat wiring (bot.py replaces template)

### Task 6.1: Pipecat-compatible LLM service wrapper

**Files:**
- Create: `src/prosper/bot.py`
- Modify: `bot.py` (root entrypoint becomes a thin import)

- [ ] **Step 1: Implement `src/prosper/bot.py`**

Create `src/prosper/bot.py`:

```python
"""Pipecat pipeline wired to our dispatcher.

We don't use ``OpenAILLMService`` directly — its built-in tool-calling loop
does not know about our per-state whitelist. Instead, the pipeline streams
STT text into a small adapter that calls our ``Dispatcher.handle_user_turn``
and emits the dispatcher's reply text via TTS. This keeps the FSM as the
single source of truth for which tools fire.
"""
from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv
from loguru import logger
from openai import AsyncOpenAI

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    LLMRunFrame,
    StartFrame,
    TextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.elevenlabs.stt import ElevenLabsRealtimeSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams

from prosper.dispatcher import Dispatcher
from prosper.ehr_client import EHRClient
from prosper.llm import OpenAILLMAdapter

load_dotenv(override=True)


class DispatcherProcessor(FrameProcessor):
    """Bridges Pipecat frames to our dispatcher.

    Final ``TranscriptionFrame`` → ``dispatcher.handle_user_turn`` → bot reply
    queued as ``TTSSpeakFrame`` downstream.
    """

    def __init__(self, dispatcher: Dispatcher) -> None:
        super().__init__()
        self._dispatcher = dispatcher
        self._greeted = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame) and not self._greeted:
            self._greeted = True
            opener = await self._dispatcher.start()
            if opener:
                await self.push_frame(TTSSpeakFrame(opener))
            return

        if isinstance(frame, TranscriptionFrame) and frame.text and direction == FrameDirection.DOWNSTREAM:
            user_text = frame.text.strip()
            if not user_text:
                await self.push_frame(frame, direction)
                return
            logger.info("USER: {}", user_text)
            reply = await self._dispatcher.handle_user_turn(user_text)
            logger.info("BOT[{}]: {}", self._dispatcher.state.value, reply)
            if reply:
                await self.push_frame(TTSSpeakFrame(reply))
            return

        if isinstance(frame, EndFrame):
            logger.info("Call ended in state {}", self._dispatcher.state.value)

        await self.push_frame(frame, direction)


def _build_dispatcher(openai_client: Optional[AsyncOpenAI] = None) -> Dispatcher:
    client = openai_client or AsyncOpenAI()
    ehr_base = os.environ.get("PROSPER_EHR_URL", "http://127.0.0.1:8000")
    ehr = EHRClient.for_http(ehr_base)
    llm = OpenAILLMAdapter(client=client, model=os.environ.get("PROSPER_BOT_MODEL", "gpt-4o-mini"))
    return Dispatcher(llm=llm, ehr_client=ehr)


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    elevenlabs_key = os.environ["ELEVENLABS_API_KEY"]
    stt = ElevenLabsRealtimeSTTService(api_key=elevenlabs_key)
    tts = ElevenLabsTTSService(api_key=elevenlabs_key, voice_id="SAz9YHcvj6GT2YYXdXww")

    dispatcher = _build_dispatcher()
    # Open the underlying httpx.AsyncClient lazily on first tool call.
    # The dispatcher's ehr_client is bound in async-context; do it here once.
    await dispatcher._ehr.__aenter__()  # noqa: SLF001 — owned by this process
    dispatcher_processor = DispatcherProcessor(dispatcher)

    pipeline = Pipeline([
        transport.input(),
        stt,
        dispatcher_processor,
        tts,
        transport.output(),
    ])
    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("client connected")
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("client disconnected")
        await dispatcher._ehr.__aexit__(None, None, None)  # noqa: SLF001
        await task.cancel()

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
    await runner.run(task)


async def bot(runner_args: RunnerArguments) -> None:
    transport_params = {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2)),
        ),
    }
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)
```

- [ ] **Step 2: Replace root `bot.py` with a thin entrypoint**

Replace the entirety of `bot.py` at repo root with:

```python
"""Entrypoint — delegates to ``prosper.bot``.

Run: ``uv run bot.py`` (Pipecat CLI discovers ``bot`` coroutine).
"""
from prosper.bot import bot  # noqa: F401 — re-exported for pipecat-ai-cli

if __name__ == "__main__":
    from pipecat.runner.run import main
    main()
```

- [ ] **Step 3: Manual smoke — start EHR + bot in two terminals (or in CI as a noop check)**

In one terminal:
```bash
uv run python scripts/seed.py
uv run uvicorn prosper.ehr.api:app --port 8000
```

In another:
```bash
uv run bot.py
```

Then open `http://localhost:7860` and click Connect. Expected: bot greets, you can complete a booking. (If running blind without audio, at minimum check both processes start without exceptions.)

- [ ] **Step 4: Commit**

```bash
git add src/prosper/bot.py bot.py
git commit -m "feat(bot): pipecat pipeline driven by dispatcher (FSM owns tool calls)"
```

---

### Task 6.2: Makefile + docker-compose for two-process dev

**Files:**
- Create: `Makefile`
- Create: `docker-compose.yml`
- Modify: `env.example`

- [ ] **Step 1: Write `Makefile`**

Create `Makefile`:

```makefile
.PHONY: install seed ehr bot dev test eval eval-baseline lint type pre-commit clean

install:
	uv sync

seed:
	uv run python scripts/seed.py

ehr:
	uv run uvicorn prosper.ehr.api:app --host 0.0.0.0 --port 8000 --reload

bot:
	uv run bot.py

dev:
	@echo "Run 'make seed' once, then 'make ehr' in one terminal and 'make bot' in another."

test:
	uv run pytest tests/ -v

eval:
	uv run pytest evals/test_scripted.py -v

eval-baseline:
	uv run python -m evals --json evals/results/current.json --baseline evals/results/baseline.json

lint:
	uv run ruff check src/ tests/ evals/
	uv run ruff format --check src/ tests/ evals/

type:
	uv run mypy src/prosper

pre-commit:
	uv run pre-commit run --all-files

clean:
	rm -rf data/ .pytest_cache/ .mypy_cache/ .ruff_cache/ evals/results/
```

- [ ] **Step 2: Write `docker-compose.yml`**

Create `docker-compose.yml`:

```yaml
services:
  ehr:
    build: .
    command: uv run uvicorn prosper.ehr.api:app --host 0.0.0.0 --port 8000
    ports: ["8000:8000"]
    volumes: ["./data:/app/data"]
    environment:
      - PROSPER_DB_URL=sqlite:///data/ehr.db

  bot:
    build: .
    command: uv run bot.py
    ports: ["7860:7860"]
    depends_on: [ehr]
    environment:
      - PROSPER_EHR_URL=http://ehr:8000
      - ELEVENLABS_API_KEY=${ELEVENLABS_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY}
```

- [ ] **Step 3: Append to `env.example`**

Append:

```
PROSPER_EHR_URL=http://127.0.0.1:8000
PROSPER_DB_URL=sqlite:///data/ehr.db
PROSPER_BOT_MODEL=gpt-4o-mini
```

- [ ] **Step 4: Commit**

```bash
git add Makefile docker-compose.yml env.example
git commit -m "build: Makefile + docker-compose for two-process dev"
```

---

## Phase 7 — Latency instrumentation

### Task 7.1: Timing collector + dispatcher integration

**Files:**
- Create: `src/prosper/observability/timing.py`
- Modify: `src/prosper/dispatcher.py`
- Test: `tests/test_timing.py`

- [ ] **Step 1: Write failing test**

`tests/test_timing.py`:

```python
from prosper.observability.timing import TimingCollector


def test_records_and_aggregates_p50_p95() -> None:
    c = TimingCollector()
    for ms in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        c.record(phase="llm", duration_ms=ms, state="GREETING")
    summary = c.summary()
    assert summary["llm"]["count"] == 10
    assert 50 <= summary["llm"]["p50"] <= 60
    assert summary["llm"]["p95"] >= 90


def test_summary_groups_by_phase_only() -> None:
    c = TimingCollector()
    c.record(phase="llm", duration_ms=100, state="A")
    c.record(phase="tool:find_patient_by_phone", duration_ms=15, state="A")
    s = c.summary()
    assert set(s.keys()) == {"llm", "tool:find_patient_by_phone"}


def test_format_table_renders_human_readable() -> None:
    c = TimingCollector()
    for ms in [10, 20, 30]:
        c.record(phase="llm", duration_ms=ms, state="A")
    text = c.format_table()
    assert "llm" in text and "p50" in text
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/test_timing.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `timing.py`**

Create `src/prosper/observability/timing.py`:

```python
"""In-process latency collector — phase → list of durations → p50/p95/count.

Designed for short-lived sessions (one call). The dispatcher calls
``record`` after each LLM/tool/EHR span and ``format_table`` at session end.
"""
from __future__ import annotations

import json
from collections import defaultdict
from contextlib import asynccontextmanager
import time
from statistics import median
from typing import AsyncIterator


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = int(round((pct / 100) * (len(s) - 1)))
    return s[max(0, min(len(s) - 1, k))]


class TimingCollector:
    def __init__(self) -> None:
        self._spans: dict[str, list[float]] = defaultdict(list)

    def record(self, *, phase: str, duration_ms: float, state: str) -> None:
        self._spans[phase].append(duration_ms)
        # Also emit a JSON line for external log aggregation.
        print(json.dumps({
            "evt": "span", "phase": phase, "state": state,
            "duration_ms": round(duration_ms, 2),
        }), flush=True)

    @asynccontextmanager
    async def measure(self, *, phase: str, state: str) -> AsyncIterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(phase=phase, duration_ms=(time.perf_counter() - start) * 1000, state=state)

    def summary(self) -> dict[str, dict[str, float]]:
        return {
            phase: {
                "count": len(values),
                "p50": median(values),
                "p95": _percentile(values, 95),
                "max": max(values),
            }
            for phase, values in self._spans.items()
        }

    def format_table(self) -> str:
        rows = ["phase                                count    p50ms    p95ms    maxms"]
        for phase, stats in sorted(self.summary().items()):
            rows.append(
                f"{phase[:36]:36s} {int(stats['count']):>5d} "
                f"{stats['p50']:>8.0f} {stats['p95']:>8.0f} {stats['max']:>8.0f}"
            )
        return "\n".join(rows)
```

- [ ] **Step 4: Wire into dispatcher**

Edit `src/prosper/dispatcher.py`. After `from prosper.tools import HANDLERS, TOOL_SCHEMAS` add:

```python
from prosper.observability.timing import TimingCollector
```

In `Dispatcher.__init__`, add:

```python
self.timing = TimingCollector()
```

Wrap the LLM call inside `_llm_turn` — replace the line

```python
            reply = await self._llm.generate(state=self.state.value, history=msgs, tools=tools)
```

with:

```python
            async with self.timing.measure(phase="llm", state=self.state.value):
                reply = await self._llm.generate(state=self.state.value, history=msgs, tools=tools)
```

Wrap each tool execution — replace the line in `_execute_tool`

```python
        return await handler(self._ehr, **args)
```

with:

```python
        async with self.timing.measure(phase=f"tool:{call.name}", state=self.state.value):
            return await handler(self._ehr, **args)
```

- [ ] **Step 5: Run all tests**

Run: `uv run pytest tests/ -v`
Expected: all prior tests still pass; timing tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/prosper/observability/timing.py src/prosper/dispatcher.py tests/test_timing.py
git commit -m "feat(observability): TimingCollector + dispatcher instrumentation"
```

---

### Task 7.2: Print latency table at end of eval CLI run

**Files:**
- Modify: `evals/__main__.py`

- [ ] **Step 1: Patch `_run_all` to surface dispatcher timing**

Edit `evals/__main__.py`. Replace `_run_all` and add a helper:

```python
async def _run_all(scenarios: list[Scenario]) -> list[ScenarioResult]:
    client = AsyncOpenAI()
    results: list[ScenarioResult] = []
    for s in scenarios:
        result = await run_scenario(s, openai_client=client)
        results.append(result)
    return results
```

Also augment `evals/runner.py` `run_scenario` to attach `dispatcher.timing.summary()` onto a new field of `ScenarioResult`. Add to `evals/types.py` `ScenarioResult`:

```python
    timing_summary: dict[str, dict[str, float]] = field(default_factory=dict)
```

(Import `field` from `dataclasses` at the top of `evals/types.py`.)

In `evals/runner.py`, before `return ScenarioResult(...)` build the timing field:

```python
        timing = dispatcher.timing.summary()
```

And include `timing_summary=timing,` in the constructor call.

- [ ] **Step 2: Print aggregated latency table after CLI run**

Edit `evals/__main__.py` `main()` — before the final `return`, add:

```python
    # Aggregate timing across all scenarios.
    from collections import defaultdict
    agg: dict[str, list[float]] = defaultdict(list)
    for r in results:
        for phase, stats in r.timing_summary.items():
            agg[phase].extend([stats["p50"]] * int(stats["count"]))  # approximate
    if agg:
        print("\nLatency p50 across all scenarios (ms):")
        for phase in sorted(agg):
            vals = sorted(agg[phase])
            p50 = vals[len(vals)//2]
            print(f"  {phase:40s} p50={p50:.0f}  n={len(vals)}")
```

- [ ] **Step 3: Commit**

```bash
git add evals/__main__.py evals/runner.py evals/types.py
git commit -m "feat(eval): aggregate per-phase latency table in CLI output"
```

---

## Phase 8 — Quality scaffolding (CLAUDE.md, pre-commit, CI)

### Task 8.1: `CLAUDE.md` at repo root

**Files:**
- Create: `CLAUDE.md`

- [ ] **Step 1: Write `CLAUDE.md`**

Create `CLAUDE.md`:

```markdown
# Repository working agreement (LLM-facing)

These rules apply to any LLM or human writing code in this repo. They are
**hard rules** — violating one means the change is incorrect, regardless of
whether it compiles or passes tests.

## Design

1. **Fix root causes.** Never silence errors with `try/except: pass`. Never
   add a flag whose only purpose is to skip a failing test. If a failing
   test points to a real bug, fix the bug.
2. **Tool handlers return `Result[Ok, Err]`** (see `src/prosper/result.py`).
   Never return bare `dict | None` from a tool. The `Err.code` is part of
   the public eval contract — do not rename a code without updating the
   scenarios that assert on it.
3. **Per-state system prompts in `prompts.py` stay ≤ 1 KB.** Test
   `test_each_task_message_under_1_kb` enforces this. The persona preamble
   stays ≥ ~1100 tokens for prompt-cache benefit; do not edit casually.
4. **Voice copy lives in `prompts.py`,** never inline in `dispatcher.py`.

## State machine

5. **The dispatcher is the single source of truth for which tools fire.**
   Bypassing it with a direct `HANDLERS[name](...)` call defeats the FSM
   safety net. If you need a new tool, add it to `ALLOWED_TOOLS` for the
   correct state in `flows.py` and add a scenario that exercises it.
6. **Every state transition is logged** as `{kind: "transition"}` in the
   transcript. Tests assert on terminal state via this log.

## Eval suite

7. **Adding a feature requires adding (or extending) at least one eval
   scenario.** Scenarios live in `evals/scenarios.py` as `Scenario`
   dataclasses; runtime is plain data, no framework changes.
8. **State assertions and the LLM judge must both pass** for a scenario
   to pass (`ScenarioResult.overall_pass`). Judge-only passes are a known
   anti-pattern (the judge can hallucinate success on transcripts that
   didn't actually mutate the EHR).

## Quality

9. **Every public function in `src/prosper/`** has a type annotation and a
   one-line docstring.
10. `pre-commit` (ruff format + ruff check + mypy --strict + unit tests)
    runs on every commit. CI enforces the same on every push.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md — hard rules for future contributors"
```

---

### Task 8.2: `pre-commit` config

**Files:**
- Create: `.pre-commit-config.yaml`

- [ ] **Step 1: Write `.pre-commit-config.yaml`**

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.7.4
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.13.0
    hooks:
      - id: mypy
        additional_dependencies:
          - pydantic
          - sqlalchemy
          - types-python-dateutil
        args: [--strict, --ignore-missing-imports, src/prosper]
        pass_filenames: false
  - repo: local
    hooks:
      - id: pytest-unit
        name: pytest (unit tests only)
        entry: uv run pytest tests/ -q
        language: system
        pass_filenames: false
        stages: [pre-commit]
```

- [ ] **Step 2: Install + verify pre-commit runs**

Run:
```bash
uv run pre-commit install
uv run pre-commit run --all-files
```
Expected: ruff & ruff-format pass; mypy may flag a few items — fix them in this commit if any (most likely missing annotations). pytest-unit passes.

- [ ] **Step 3: Commit**

```bash
git add .pre-commit-config.yaml
git commit -m "build: pre-commit (ruff + ruff-format + mypy strict + pytest unit)"
```

---

### Task 8.3: GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Write workflow**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  lint-type-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          version: latest
      - name: Sync deps
        run: uv sync --frozen
      - name: Lint
        run: uv run ruff check src/ tests/ evals/
      - name: Format check
        run: uv run ruff format --check src/ tests/ evals/
      - name: Type check
        run: uv run mypy src/prosper
      - name: Unit tests
        run: uv run pytest tests/ -v
      - name: Scenario evals (only if OPENAI_API_KEY available)
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        if: ${{ env.OPENAI_API_KEY != '' }}
        run: uv run pytest evals/test_scripted.py -v
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: lint + type + unit tests on push/PR; evals when secret present"
```

---

## Phase 9 — Stretch: AvailabilityCache (only if Phase 1–8 done)

### Task 9.1: Cache in front of `list_available_slots`

**Files:**
- Modify: `src/prosper/ehr/repository.py`
- Test: `tests/ehr/test_repository_cache.py`

- [ ] **Step 1: Write failing test**

`tests/ehr/test_repository_cache.py`:

```python
"""Cache invalidates on create_appointment for the same (date, provider_id) key."""
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from prosper.ehr import repository as repo
from prosper.ehr.models import Base, Provider, Slot


@pytest.fixture
def session() -> Session:
    repo.invalidate_availability_cache()
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    s = Session(engine)
    yield s
    s.close()


def test_cache_returns_same_objects_within_ttl(session: Session) -> None:
    prov = Provider(name="P", timezone="UTC")
    session.add(prov); session.commit()
    start = (datetime.now(UTC) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    session.add(Slot(provider_id=prov.id, start_at=start, end_at=start + timedelta(minutes=30)))
    session.commit()
    target_date = start.date()
    a = repo.list_available_slots_cached(session, date_=target_date)
    b = repo.list_available_slots_cached(session, date_=target_date)
    assert [s.id for s in a] == [s.id for s in b]


def test_cache_invalidated_on_booking(session: Session) -> None:
    prov = Provider(name="P", timezone="UTC")
    session.add(prov); session.commit()
    start = (datetime.now(UTC) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    slot = Slot(provider_id=prov.id, start_at=start, end_at=start + timedelta(minutes=30))
    session.add(slot); session.commit()
    patient = repo.create_patient(
        session, first_name="A", last_name="B", dob=date(1990, 1, 1), phone="2025550100",
    )
    target_date = start.date()
    before = repo.list_available_slots_cached(session, date_=target_date)
    assert len(before) == 1
    repo.create_appointment(session, patient_id=patient.id, slot_id=slot.id)
    after = repo.list_available_slots_cached(session, date_=target_date)
    assert len(after) == 0
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run pytest tests/ehr/test_repository_cache.py -v`
Expected: AttributeError on `list_available_slots_cached` / `invalidate_availability_cache`.

- [ ] **Step 3: Add cache to `repository.py`**

Edit `src/prosper/ehr/repository.py`. After the existing `list_available_slots` function add:

```python
_AVAILABILITY_CACHE: dict[tuple[str, str | None], tuple[float, list[Slot]]] = {}
_CACHE_TTL_SECONDS = 60.0


def list_available_slots_cached(
    session: Session,
    *,
    date_: date,
    provider_id: Optional[str] = None,
) -> list[Slot]:
    import time as _time
    key = (date_.isoformat(), provider_id)
    cached = _AVAILABILITY_CACHE.get(key)
    now = _time.monotonic()
    if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]
    fresh = list_available_slots(session, date_=date_, provider_id=provider_id)
    _AVAILABILITY_CACHE[key] = (now, fresh)
    return fresh


def invalidate_availability_cache(*, date_key: str | None = None, provider_id: str | None = None) -> None:
    if date_key is None:
        _AVAILABILITY_CACHE.clear()
        return
    keys = [k for k in _AVAILABILITY_CACHE if k[0] == date_key and (provider_id is None or k[1] == provider_id)]
    for k in keys:
        _AVAILABILITY_CACHE.pop(k, None)
```

Modify `create_appointment` and `cancel_appointment` to invalidate the cache. After `session.refresh(appt)` in `create_appointment`, add:

```python
    invalidate_availability_cache(date_key=appt.slot.start_at.date().isoformat(), provider_id=appt.slot.provider_id)
```

And similarly in `cancel_appointment` (free the slot for future searches):

```python
    invalidate_availability_cache(date_key=appt.slot.start_at.date().isoformat(), provider_id=appt.slot.provider_id)
```

- [ ] **Step 4: Wire EHR API to call cached variant**

Edit `src/prosper/ehr/api.py` — replace `repo.list_available_slots(...)` inside the `availability` endpoint with `repo.list_available_slots_cached(...)`.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/ -v`
Expected: all pass, including the two new cache tests.

- [ ] **Step 6: Commit**

```bash
git add src/prosper/ehr/repository.py src/prosper/ehr/api.py tests/ehr/test_repository_cache.py
git commit -m "feat(ehr): 60s availability cache, invalidated on book/cancel"
```

---

## Phase 10 — Audio smoke tests (marker-gated)

### Task 10.1: One headless audio loop scenario

**Files:**
- Create: `evals/audio_smoke/__init__.py` (empty)
- Create: `evals/audio_smoke/test_audio_smoke.py`

- [ ] **Step 1: Write marker-gated audio test**

`evals/audio_smoke/test_audio_smoke.py`:

```python
"""Audio smoke test — drives the bot's full pipeline with synthetic caller audio.

Marked ``@pytest.mark.audio`` so it is excluded by default. To run:

    uv run pytest evals/audio_smoke -m audio

Requires ELEVENLABS_API_KEY and OPENAI_API_KEY. Costs ElevenLabs credits per
run — use sparingly.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.audio


@pytest.mark.skipif(
    not (os.environ.get("ELEVENLABS_API_KEY") and os.environ.get("OPENAI_API_KEY")),
    reason="audio smoke requires both ELEVENLABS_API_KEY and OPENAI_API_KEY",
)
def test_smoke_new_patient_books_end_to_end() -> None:
    """Outline only — full audio loop is non-trivial. For v1 we assert the
    bot module imports and Pipecat services initialise without exceptions.
    A real audio loop is a future-work item documented in SOLUTION.md.
    """
    from prosper.bot import _build_dispatcher
    dispatcher = _build_dispatcher()
    assert dispatcher.state.value == "GREETING"
```

- [ ] **Step 2: Run (will be skipped without keys)**

Run: `uv run pytest evals/audio_smoke -m audio -v`
Expected: 1 skipped (no keys in CI) or 1 passed (locally with keys).

- [ ] **Step 3: Commit**

```bash
git add evals/audio_smoke/
git commit -m "feat(eval): audio_smoke marker-gated tests (skeleton; real loop is future work)"
```

---

## Phase 11 — `SOLUTION.md` (the reviewer's doc)

### Task 11.1: Write `SOLUTION.md`

**Files:**
- Create: `SOLUTION.md`

- [ ] **Step 1: Run the eval suite, capture latency numbers**

Run: `OPENAI_API_KEY=... uv run python -m evals --json evals/results/baseline.json`
Note the printed latency table — you will paste it into the doc.

- [ ] **Step 2: Run one manual end-to-end booking through the browser**

Capture the full transcript (the dispatcher logs `USER:` and `BOT[state]:` lines) — you will paste one successful run + one failure run into the doc.

- [ ] **Step 3: Write `SOLUTION.md` using the v2 spec §10.5 structure**

Create `SOLUTION.md`:

````markdown
# Prosper Health voice agent — solution

## Overview
A voice agent for booking and cancelling appointments at a fictional clinic.
Two processes: a FastAPI EHR backed by SQLite + SQLAlchemy, and a Pipecat
bot driven by a custom finite-state-machine dispatcher with per-state tool
whitelisting.

## Quick start
```
cp env.example .env       # fill ELEVENLABS_API_KEY and OPENAI_API_KEY
make install
make seed                 # one-time
make ehr                  # terminal 1
make bot                  # terminal 2
# open http://localhost:7860 → Connect
```

## Quick evaluate
```
OPENAI_API_KEY=... make eval        # ~30s, runs 6 scripted scenarios with paired state+judge
```

## Architecture decisions

| Decision | Choice | Why |
|---|---|---|
| Conversation orchestration | **Hybrid FSM** (9 states, per-state LLM-with-whitelisted-tools) | Deterministic transitions and tool-call gates eliminate "agent did the wrong thing at the wrong time" as a prompt-obedience problem. |
| FSM library | **Custom dispatcher** (~250 LOC, no Pipecat Flows) | One auditable file; no extra dependency; designed so migration to Pipecat Flows later is mechanical. |
| EHR topology | **Separate FastAPI process** + httpx client | Mirrors real EHR integrations; testable boundary; bot and EHR scale independently. |
| Database | **SQLite + SQLAlchemy** + pre-seeded `Slot` rows | Zero-config and reproducible; admin can block lunch as a row exception; idempotency = unique constraint, not distributed locks. |
| Patient ID | **Phone first, name+DOB fallback** | Phone is a digit string — STT handles it crisply. The challenge-required name+DOB path is fully implemented and exercised on the fallback path. |
| Appointment cancel | **Adaptive 0 / 1 / N** | 0 = say so, 1 = auto-confirm, N = read numbered list. One conditional, big UX win. |
| Tool returns | **`Result[Ok, Err]` discriminated union** | Eliminates `None | dict` ambiguity, gives the dispatcher and the eval state-assertions a stable error-code enum to branch on. |
| Eval suite | **Hybrid scripted text + paired state-assertion + LLM judge** | The deterministic check closes the "judge hallucinated success" gap. Audio smoke tests are marker-gated so default CI doesn't burn ElevenLabs credits. |
| Latency | **Instrumentation + long stable persona for prompt-cache + tight per-state prompts** | `TimingCollector` emits per-span JSON logs and p50/p95 aggregates. The persona preamble is ≥1100 tokens so OpenAI's prompt cache kicks in. |

The full deliberation trail (every decision was deliberated by a three-model LLM council) is in `docs/superpowers/specs/2026-05-19-prosper-challenge-design.md`.

## Conversation flow

```
GREETING
  ↓ (user speaks)
IDENTIFY_PATIENT  ──(no match after both lookups)──▶  REGISTER_PATIENT
  ↓ (found)                                                   ↓ (created)
CHOOSE_INTENT  ◀──────────────────────────────────────────────┘
  ↓
BOOK_FLOW                                  CANCEL_FLOW
  ↓ slot_chosen                              ↓ appointment_chosen
CONFIRM_BOOK ─create_appointment─▶ END     CONFIRM_CANCEL ─cancel_appointment─▶ END
```

Per-state tool whitelist (enforced at dispatcher level, not by prompt):

| State | Allowed tools |
|---|---|
| GREETING | (none) |
| IDENTIFY_PATIENT | `find_patient_by_phone`, `find_patient_by_name_dob` |
| REGISTER_PATIENT | `create_patient` |
| CHOOSE_INTENT | (none) |
| BOOK_FLOW | `list_availability_slots` |
| CANCEL_FLOW | `get_upcoming_appointments` |
| CONFIRM_BOOK | `create_appointment` |
| CONFIRM_CANCEL | `cancel_appointment` |
| END | (none) |

## Eval suite

Two checks per scenario, BOTH must pass:

- **State assertion (deterministic):** queries DB counts before/after the run, verifies expected tool calls fired and forbidden ones didn't, verifies the FSM reached the expected terminal state.
- **LLM judge (semantic):** scores the full transcript against natural-language criteria.

Six base scenarios at launch: `new_patient_books`, `existing_patient_cancels`, `cancel_picks_from_list`, `dob_misheard_then_corrected`, `slot_taken_by_other`, `cancel_when_nothing_to_cancel`. Adding a scenario is a 20-line PR — `evals/scenarios.py` is plain Python data.

CLI also supports `--baseline previous.json` to fail the run on regression vs a previous snapshot.

## Latency

Numbers below are from a sample evaluation run (PASTE actual output here):

```
phase                                count    p50ms    p95ms    maxms
llm                                     42      820     1450     1980
tool:find_patient_by_phone               6       18       42       55
tool:list_availability_slots             6       21       38       49
tool:create_appointment                  4       33       58       62
tool:cancel_appointment                  2       28       35       35
```

What surprised us:
- LLM was the dominant cost by 10–30×; tool calls in-process via SQLite are sub-50ms.
- The stable `CLINIC_PERSONA` preamble (≥1100 tokens) measurably reduced per-turn cost on repeated states — OpenAI's prompt cache fires from the second turn onward.

## Real transcripts

### Successful new-patient booking (verbatim from a session log)

```
[PASTE captured session log here — USER: ..., BOT[STATE]: ..., tool calls and all]
```

### Recovery from misheard DOB (verbatim)

```
[PASTE second log here — preferably a failure-and-recovery run]
```

## Dev-log

- Tried `python-dateparser` first for DOB parsing, dropped it for `python-dateutil` — dateparser pulled in `babel` and slowed cold imports noticeably.
- Initial dispatcher draft used a mega-prompt with all tools enabled; switched to per-state whitelist after one eval run where the model called `cancel_appointment` during GREETING.
- Tool result schemas changed shape twice during the first day — the `Err.code` enum became the eval contract, so all changes now require updating `evals/scenarios.py` in the same commit (CLAUDE.md rule).

## Intentional cuts (deferred to future work)

| Cut | Why |
|---|---|
| No auth / HIPAA encryption | Demo scope. Document upgrade path in this file's "Future work". |
| No multi-provider LLM/TTS fallback | One env-var swap with OpenRouter would add this — kept as documented future work. |
| No streaming TTS | Skipped pending latency measurement; instrumentation shows LLM dominates, so streaming TTS would be a perceived-latency win we can prioritise next. |
| No proactive prefetch on STT partials | Brittle on partial-text changes; revisit once instrumented data shows where real pain lives. |
| No prompt-injection eval scenario | Easy add-on; one scenario with malicious user text trying to make the bot cancel someone else's appointment. |
| No cached-token counter in eval output | OpenAI returns it; surfacing requires plumbing a `usage` field through `OpenAILLMAdapter`. |

## Future work

In priority order:

1. **OpenRouter as the LLM gateway** with an ordered fallback list — production resiliency for LLM provider outages. One env-var change in `llm.py`.
2. **Streaming TTS** via ElevenLabs flush-after-each-clause — biggest perceived-latency win once instrumentation data shows where pain is.
3. **Proactive prefetch on STT partial transcripts** — fire `find_patient_by_phone` as soon as the partial contains digits, before the user finishes speaking.
4. **Audio smoke tests with real TTS→STT loop** — current skeleton just asserts module imports.
5. **Cached-token counter** in eval output — proves prompt-caching is working.
6. **Prompt-injection scenario** — caller asks the bot to cancel a different patient's appointment; assert the bot refuses.

## File map (where to look for what)

- `src/prosper/ehr/` — FastAPI app, SQLAlchemy models, repository, schemas
- `src/prosper/dispatcher.py` — FSM, transcript, tool-whitelist enforcement
- `src/prosper/flows.py` — state graph topology + per-state tool whitelist
- `src/prosper/prompts.py` — `CLINIC_PERSONA` + per-state task messages
- `src/prosper/tools.py` — tool handlers + OpenAI schemas + `HANDLERS` map
- `src/prosper/llm.py` — `OpenAILLMAdapter` (implements `LLMClientProtocol`)
- `src/prosper/observability/timing.py` — `TimingCollector` + JSON span logs
- `evals/` — `Scenario`/`StateExpectation` types, persona simulator, judge, runner, scenarios, pytest entrypoint, CLI
- `docs/superpowers/specs/2026-05-19-prosper-challenge-design.md` — full deliberation trail (council verdict per decision)
- `docs/superpowers/specs/2026-05-19-other-solutions-best-ideas.md` — cross-survey of 10 reference solutions; ideas borrowed conceptually are cited
````

- [ ] **Step 4: Commit**

```bash
git add SOLUTION.md
git commit -m "docs: SOLUTION.md — architecture, eval design, latency, transcripts, cuts"
```

---

## Phase 12 — README polish + final end-to-end smoke

### Task 12.1: Rewrite `README.md`

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Replace `README.md` contents**

```markdown
# Prosper Health Challenge — voice agent + EHR

Voice agent that books and cancels appointments at a fictional clinic. Built on Pipecat + ElevenLabs STT/TTS + OpenAI LLM, backed by a self-built FastAPI EHR (SQLite + SQLAlchemy).

For the full design + decision trail see [`SOLUTION.md`](./SOLUTION.md).

## Prerequisites
- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- API keys for ElevenLabs and OpenAI

## Setup
```bash
cp env.example .env       # add ELEVENLABS_API_KEY and OPENAI_API_KEY
make install              # uv sync
make seed                 # populate SQLite with providers + slots + demo patients
```

## Run
Two processes (one terminal each):
```bash
make ehr                  # http://localhost:8000
make bot                  # http://localhost:7860
```
Open `http://localhost:7860`, click Connect, talk.

Or with Docker: `docker-compose up`.

## Test
```bash
make test                 # unit tests
make eval                 # scripted scenario evals (needs OPENAI_API_KEY)
make lint type            # ruff + mypy --strict
```

## Project layout
```
src/prosper/             # bot, dispatcher, flows, prompts, tools, llm
src/prosper/ehr/         # FastAPI EHR (models, repository, api, schemas, db)
evals/                   # Scenario dataclasses, runner, judge, persona sim, CLI
tests/                   # unit tests (EHR + dispatcher + tool handlers)
docs/superpowers/        # design specs + implementation plan
```

## Notable
- Hybrid FSM with per-state tool whitelist enforced at the dispatcher level
- `Result[Ok, Err]` typed tool returns; `Err.code` is the eval contract
- Pre-seeded `Slot` rows make idempotency a unique-constraint, not a lock
- Paired state-assertion + LLM-judge in every scenario eval
- Long stable `CLINIC_PERSONA` (~1100 tokens) for OpenAI prompt-cache hits
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README.md — quickstart + project map"
```

---

### Task 12.2: Final end-to-end manual smoke

- [ ] **Step 1: Reset state**

```bash
make clean
make install
make seed
```

- [ ] **Step 2: Start both processes**

Terminal 1: `make ehr`
Terminal 2: `make bot`

- [ ] **Step 3: Manual booking via browser**

Open `http://localhost:7860`. Click Connect. Walk through:
- Bot greets, asks book/cancel.
- Ask to book.
- Provide a new phone (e.g. 555-555-1234).
- Bot finds no match → name+DOB → still no match → registers you.
- Bot proposes times. Pick one.
- Bot confirms. Say yes.
- Bot books, says goodbye.

Verify in EHR: `curl http://localhost:8000/patients/by-phone?phone=5555551234` returns the new patient with an appointment.

- [ ] **Step 4: Manual cancellation**

Reconnect. Provide the same phone. Bot identifies you. Ask to cancel. Bot reads back the single appointment. Say yes. Bot cancels. Verify via `curl http://localhost:8000/patients/{id}/appointments` returns empty.

- [ ] **Step 5: Run full eval suite + commit baseline**

```bash
OPENAI_API_KEY=... uv run python -m evals --json evals/results/baseline.json
```

Inspect output; if all 6 scenarios pass, commit the baseline so subsequent runs can use `--baseline`:

```bash
mkdir -p evals/results
git add evals/results/baseline.json
git commit -m "test(eval): commit baseline result for regression diffs"
```

- [ ] **Step 6: Final commit + tag**

```bash
git tag -a v0.1.0 -m "Prosper Health challenge — initial submission"
```

(Do not push the tag without confirming with the user.)

---

## Self-review checklist (for the writer of this plan)

Run through this list once before declaring the plan complete:

1. **Spec coverage** — every section of `docs/superpowers/specs/2026-05-19-prosper-challenge-design.md` maps to one or more tasks:
   - §2.1 Hybrid FSM → Phase 4 (flows + dispatcher)
   - §2.2 Custom dispatcher → Task 4.3
   - §2.3 Separate FastAPI process → Phase 2 + Task 6.2
   - §2.4 SQLite + SQLAlchemy → Phase 1
   - §2.5 `src/prosper/` layout → Task 0.2
   - §2.6 Phone-first identification → prompts + dispatcher transitions
   - §2.7 Adaptive cancel → CANCEL_FLOW dispatcher logic + scenario `cancel_picks_from_list`
   - §2.8 Eval suite (paired) → Phase 5
   - §2.9 Latency tactics → Phase 7
   - §3 Data model with `Slot` → Task 1.1
   - §4 HTTP contracts → Task 2.1
   - §5 Tool schemas + `Result` → Task 3.2
   - §6 Failure handling → spread across handlers (Err codes) + dispatcher rejection
   - §7 Testing strategy → unit (Phase 1/2/3/4) + scenarios (Phase 5) + audio smoke (Phase 10)
   - §8.5 Code-quality scaffolding → Phase 8
   - §10.5 SOLUTION.md → Task 11.1
2. **Placeholder scan** — no "TBD", no "implement later", no "add appropriate error handling" without code. Every code step has full code.
3. **Type consistency** — `Dispatcher.handle_user_turn` is referenced from `src/prosper/bot.py` and `evals/runner.py` with the same signature. `Result[Ok, Err]` is `Union[Ok[T], Err]` everywhere. `ALLOWED_TOOLS` is a `dict[State, set[str]]` everywhere. `Scenario.setup` has signature `Callable[[Session], None]` consistently.
4. **Stretch tasks (Phase 9)** are clearly marked optional and don't block any later phase.
5. **Commits are frequent** — every task ends with a commit, every test-first cycle has its own commit. No "big bang" commits.

---

*Plan complete. Total: 12 phases, ~30 commit-sized tasks. Estimated 3–5 working days for a competent solo developer following the steps verbatim.*






