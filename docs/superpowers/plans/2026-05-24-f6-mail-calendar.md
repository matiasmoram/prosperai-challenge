# F6 — Mail + Calendar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A staff-facing Mail + Calendar surface (front F6) that emulates the clinic's outbound mail — booking confirmations to the caller (automatic, off-spine) and human handoffs to the front desk (LLM tool + safety-net, on-spine) — plus an EHR-backed appointment calendar, served at `/frontdesk` on the console uvicorn.

**Architecture:** Both mail channels write a `MailMessage` to one full-PII `MailStore` (separate trust tier from the masked operator-console bus, module `src/prosper/integrations/`). Booking confirmations fire as a fire-and-forget dispatcher side-effect after `create_appointment` Ok (off-spine). Handoffs fire via a new `leave_message_for_front_desk` tool (dispatcher-intercepted, identity from `SessionMemory` not LLM args) plus a dispatcher safety-net on loop exhaustion; a new terminal `HANDOFF` state models the outcome (on-spine, crosses S1/S3). The `/frontdesk` SPA polls mail every 2 s and reads the calendar from a new EHR `GET /appointments` endpoint.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, httpx, aiofiles, pytest (`asyncio_mode=auto`), vanilla HTML/JS, `mypy --strict`, ruff.

**Spec:** `docs/superpowers/specs/2026-05-24-f6-mail-calendar-design.md`
**Front:** F6 (Mail + Calendar) — `FRONTS.md`. Module home `src/prosper/integrations/`.

**Conventions (CLAUDE.md):** tools return `Result[Ok, Err]` (`Err.code` is public contract); the dispatcher is the only path to tools; telemetry/mail writes never break the call path (`try/except`); EHR is source of truth; every public function has a type annotation + one-line docstring; `make verify` + `make mock-eval` stay green; run everything with `uv run`.

**Spine / ownership (FRONTS.md):** This front touches three seams — F1 (`ehr/**`, Task 1), F2 spine S1/S3 (`tools.py`/`flows.py`/`dispatcher.py`/`bot.py`, Tasks 3-6,10), F3 (`prompts.py`, Task 7), F7 (`evals/**`, Task 8). **Only one agent edits the spine at a time.** Do not run this concurrently with another F2-spine front. Rebase additively; never reorder existing `ALLOWED_TOOLS`/`TRANSITIONS`/`TOOL_SCHEMAS` entries.

---

## File Structure

**New (F6-owned):**
- `src/prosper/integrations/__init__.py` — package marker + exports.
- `src/prosper/integrations/mail.py` — `MailMessage` dataclass, `make_message`, `MailStore`.
- `src/prosper/integrations/router.py` — `/frontdesk` router (`build_frontdesk_router`).
- `src/prosper/integrations/static/index.html` — Mail + Calendar SPA shell.
- `src/prosper/integrations/static/frontdesk.js` — 2 s mail poll, render message + calendar.
- `tests/integrations/__init__.py`
- `tests/integrations/test_mail_store.py`
- `tests/integrations/test_router.py`
- `tests/integrations/test_calendar_endpoint.py`
- `tests/test_mail_dispatcher.py` — handoff tool, confirmation side-effect, safety-net.
- `docs/adr/005-f6-mail-calendar.md`

**Modified:**
- F1: `ehr/repository.py`, `ehr/schemas.py`, `ehr/api.py`, `ehr_client.py`.
- F2 spine: `flows.py`, `tools.py`, `dispatcher.py`, `bot.py`, `console/server.py`.
- F3: `prompts.py`.
- F7: `evals/scenarios.py`, `evals/mock_llm.py`.
- Docs: `FRONTS.md` (§F6), `SOLUTION.md`, `CLAUDE.md`.

---

## Task 1: EHR calendar read endpoint  *(F1 — coordinate)*

**Files:** Modify `ehr/repository.py`, `ehr/schemas.py`, `ehr/api.py`, `ehr_client.py`; Create `tests/integrations/__init__.py`, `tests/integrations/test_calendar_endpoint.py`.

- [ ] **Step 1: Create the test package marker**

Create `tests/integrations/__init__.py` (empty).

- [ ] **Step 2: Write the failing endpoint test**

Mirror the app-construction pattern in `tests/ehr/test_api.py` (in-memory SQLite engine + `create_app(engine=...)` + `TestClient`). Create `tests/integrations/test_calendar_endpoint.py`:

```python
"""GET /appointments?from=&to= — clinic-wide calendar read."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from prosper.ehr.api import create_app
from prosper.ehr.models import Appointment, AppointmentStatus, Patient, Provider, Slot


def _engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


def _seed(engine) -> None:
    start = datetime(2026, 5, 26, 14, 0, tzinfo=timezone.utc)  # naive-UTC seed convention
    with Session(engine) as s:
        s.add_all([
            Provider(id="p1", name="Dr. Patel", timezone="UTC", specialty="Therapist"),
            Patient(id="pt1", first_name="Jane", last_name="Doe", name_normalized="jane doe",
                    dob=date(1990, 1, 1), phone="2025550142"),
            Slot(id="s1", provider_id="p1", start_at=start, end_at=start + timedelta(minutes=30)),
            Appointment(id="a1", patient_id="pt1", slot_id="s1",
                        status=AppointmentStatus.SCHEDULED, duration_minutes=30, notes="knee pain"),
        ])
        s.commit()


def test_calendar_returns_scheduled_in_range():
    engine = _engine(); app = create_app(engine=engine); _seed(engine)
    resp = TestClient(app).get("/appointments", params={"from": "2026-05-25", "to": "2026-05-27"})
    assert resp.status_code == 200
    e = resp.json()["entries"][0]
    assert e["patient_name"] == "Jane Doe"
    assert e["provider_name"] == "Dr. Patel"
    assert e["specialty"] == "Therapist"
    assert e["notes"] == "knee pain"
    assert e["status"] == "scheduled"


def test_calendar_excludes_out_of_range():
    engine = _engine(); app = create_app(engine=engine); _seed(engine)
    resp = TestClient(app).get("/appointments", params={"from": "2026-05-01", "to": "2026-05-02"})
    assert resp.status_code == 200
    assert resp.json()["entries"] == []
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/integrations/test_calendar_endpoint.py -v`
Expected: FAIL — 404 / KeyError `entries`.

- [ ] **Step 4: Add the schemas**

In `ehr/schemas.py`, after `AppointmentList` (line 161):

```python
class CalendarEntryOut(BaseModel):
    """One appointment as the staff calendar needs it (patient name + reason)."""

    appointment_id: str
    patient_name: str
    provider_name: str
    specialty: str
    start_at: datetime
    end_at: datetime
    duration_minutes: int
    status: str
    notes: str | None = None


class CalendarList(BaseModel):
    """Range response for the staff calendar."""

    entries: list[CalendarEntryOut]
```

- [ ] **Step 5: Add the repository query**

In `ehr/repository.py` (reuse existing imports; check whether `date` is imported plain or as `date_t` and match it):

```python
def list_appointments_in_range(
    session: Session,
    *,
    from_date: date_t,
    to_date: date_t,
) -> list[tuple[Appointment, Patient, Provider]]:
    """Scheduled appointments whose slot starts within [from_date, to_date].

    Joined with patient + provider so the staff calendar renders names without
    a lazy-load per row. ``to_date`` is inclusive. Slot datetimes are naive UTC.
    """
    day_start = datetime(from_date.year, from_date.month, from_date.day, tzinfo=timezone.utc)
    day_end = datetime(to_date.year, to_date.month, to_date.day, tzinfo=timezone.utc) + timedelta(days=1)
    stmt = (
        select(Appointment, Patient, Provider)
        .join(Slot, Slot.id == Appointment.slot_id)
        .join(Patient, Patient.id == Appointment.patient_id)
        .join(Provider, Provider.id == Slot.provider_id)
        .where(Appointment.status == AppointmentStatus.SCHEDULED)
        .where(Slot.start_at >= day_start)
        .where(Slot.start_at < day_end)
        .order_by(Slot.start_at)
    )
    return [tuple(row) for row in session.execute(stmt).all()]
```

- [ ] **Step 6: Add the API endpoint**

In `ehr/api.py`, add `CalendarEntryOut, CalendarList` to the schema import block (lines 24-37), then inside `create_app` (after `patient_appointments`):

```python
    @app.get("/appointments", response_model=CalendarList)
    def calendar(
        from_: date_t = Query(alias="from"),
        to: date_t = Query(...),
        session: Session = Depends(session_dep),
    ) -> CalendarList:
        rows = repo.list_appointments_in_range(session, from_date=from_, to_date=to)
        return CalendarList(entries=[
            CalendarEntryOut(
                appointment_id=a.id,
                patient_name=f"{p.first_name} {p.last_name}".strip(),
                provider_name=prov.name,
                specialty=prov.specialty,
                start_at=a.slot.start_at,
                end_at=a.slot.start_at + timedelta(minutes=a.duration_minutes),
                duration_minutes=a.duration_minutes,
                status=a.status.value,
                notes=a.notes,
            )
            for (a, p, prov) in rows
        ])
```

`timedelta` (line 8), `date_t` (line 7), `Query` (line 10) are already imported.

- [ ] **Step 7: Run to verify pass**

Run: `uv run pytest tests/integrations/test_calendar_endpoint.py -v`
Expected: PASS (2).

- [ ] **Step 8: Add the EHR client method**

In `ehr_client.py`, after `get_upcoming_appointments` (line 168):

```python
    async def list_appointments_in_range(
        self, *, from_date: date, to_date: date
    ) -> list[dict[str, Any]]:
        """Fetch staff-calendar entries for [from_date, to_date]."""
        body = await self._request(
            "GET", "/appointments",
            params={"from": from_date.isoformat(), "to": to_date.isoformat()},
        )
        return cast("list[dict[str, Any]]", body["entries"])
```

(`date`, `Any`, `cast` imported at the top — add `from datetime import date` only if absent.)

- [ ] **Step 9: Type-check**

Run: `uv run mypy --strict src/prosper/ehr src/prosper/ehr_client.py`
Expected: no errors.

- [ ] **Step 10: Commit**

```bash
git add src/prosper/ehr/repository.py src/prosper/ehr/schemas.py src/prosper/ehr/api.py src/prosper/ehr_client.py tests/integrations/__init__.py tests/integrations/test_calendar_endpoint.py
git commit -m "feat(ehr): clinic-wide calendar read endpoint (GET /appointments)"
```

---

## Task 2: MailStore + MailMessage  *(F6)*

**Files:** Create `src/prosper/integrations/__init__.py`, `src/prosper/integrations/mail.py`, `tests/integrations/test_mail_store.py`.

- [ ] **Step 1: Write the failing store test**

Create `tests/integrations/test_mail_store.py`:

```python
"""MailStore: append-only full-PII outbound-mail records."""

from __future__ import annotations

import pytest

from prosper.integrations.mail import MailMessage, MailStore, make_message


@pytest.fixture
def store(tmp_path) -> MailStore:
    return MailStore(root=tmp_path)


async def test_write_then_list_roundtrip(store: MailStore) -> None:
    await store.write(make_message(
        session_id="s1", kind="handoff", to_label="reception@prosper.health",
        subject="Callback — Jane Doe", body="Wants a refill on metformin.",
        patient_name="Jane Doe", patient_phone="(202) 555-0142",
        category="prescription", ts=1000.0,
    ))
    got = store.list_messages()
    assert len(got) == 1
    assert got[0].kind == "handoff"
    assert got[0].category == "prescription"
    assert got[0].patient_phone == "(202) 555-0142"  # full PII, NOT masked


async def test_list_is_newest_first(store: MailStore) -> None:
    for i, ts in enumerate([1000.0, 3000.0, 2000.0]):
        await store.write(make_message(
            session_id=f"s{i}", kind="booking_confirmation", to_label="A B",
            subject="x", body="y", patient_name="A B", patient_phone="2025550000", ts=ts,
        ))
    assert [m.ts for m in store.list_messages()] == [3000.0, 2000.0, 1000.0]


async def test_list_empty_when_no_writes(store: MailStore) -> None:
    assert store.list_messages() == []
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/integrations/test_mail_store.py -v`
Expected: FAIL — `ModuleNotFoundError: prosper.integrations`.

- [ ] **Step 3: Create the package marker**

Create `src/prosper/integrations/__init__.py`:

```python
"""F6 — Mail + Calendar: outbound-mail store + staff web router."""

from prosper.integrations.mail import MailMessage, MailStore, make_message

__all__ = ["MailMessage", "MailStore", "make_message"]
```

- [ ] **Step 4: Implement the store**

Create `src/prosper/integrations/mail.py` (model the JSONL/handle shape on `console/audit.py`, but **no masking** — staff tier):

```python
"""Durable, full-PII outbound-mail records for the front-desk surface.

One JSONL file per session at ``<root>/<session_id>.jsonl``. Unlike the
operator console, records carry the patient's real name + phone — a callback
or a confirmation is useless masked. Deliberately a different trust tier: the
``/frontdesk`` surface that reads it is staff-only (behind auth in prod, loopback
in the demo). Two channels share one record via ``kind``. See spec §7.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

import aiofiles

_DEFAULT_ROOT_NAME: Final[str] = "data/mail"


def _resolve_default_root() -> Path:
    """Compute the default mail root, honouring ``PROSPER_MAIL_ROOT``."""
    override = os.environ.get("PROSPER_MAIL_ROOT")
    return Path(override) if override else Path(_DEFAULT_ROOT_NAME)


@dataclass(frozen=True, slots=True)
class MailMessage:
    """One simulated outbound message. Full PII — staff tier only."""

    ts: float
    session_id: str
    kind: str  # "booking_confirmation" | "handoff" | "bot_failed"
    to_label: str
    subject: str
    body: str
    patient_name: str
    patient_phone: str
    category: str = ""  # handoff only; "" otherwise

    def to_json(self) -> str:
        """Serialise to one JSONL line."""
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "MailMessage":
        """Inverse of ``to_json``."""
        d = json.loads(raw)
        return cls(
            ts=float(d["ts"]),
            session_id=str(d["session_id"]),
            kind=str(d["kind"]),
            to_label=str(d["to_label"]),
            subject=str(d["subject"]),
            body=str(d["body"]),
            patient_name=str(d["patient_name"]),
            patient_phone=str(d["patient_phone"]),
            category=str(d.get("category", "")),
        )


def make_message(
    *,
    session_id: str,
    kind: str,
    to_label: str,
    subject: str,
    body: str,
    patient_name: str,
    patient_phone: str,
    category: str = "",
    ts: float | None = None,
) -> MailMessage:
    """Build a ``MailMessage``; ``ts`` defaults to ``time.time()``."""
    return MailMessage(
        ts=time.time() if ts is None else ts,
        session_id=session_id,
        kind=kind,
        to_label=to_label,
        subject=subject,
        body=body,
        patient_name=patient_name,
        patient_phone=patient_phone,
        category=category,
    )


class MailStore:
    """Append-only JSONL store of outbound mail, one file per session."""

    def __init__(self, root: Path | None = None) -> None:
        """Initialise with a target root dir (default ``data/mail/``)."""
        self._root: Path = root if root is not None else _resolve_default_root()

    @property
    def root(self) -> Path:
        """Mail root directory."""
        return self._root

    async def write(self, message: MailMessage) -> None:
        """Append ``message`` as one JSON line to its session file."""
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / f"{message.session_id}.jsonl"
        async with aiofiles.open(path, mode="a", encoding="utf-8") as handle:
            await handle.write(message.to_json() + "\n")
            await handle.flush()

    def list_messages(self) -> list[MailMessage]:
        """All messages across all sessions, newest first (by ``ts``)."""
        if not self._root.exists():
            return []
        out: list[MailMessage] = []
        for path in self._root.glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped:
                    out.append(MailMessage.from_json(stripped))
        out.sort(key=lambda m: m.ts, reverse=True)
        return out
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/integrations/test_mail_store.py -v`
Expected: PASS (3).

- [ ] **Step 6: Type-check**

Run: `uv run mypy --strict src/prosper/integrations`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/prosper/integrations/__init__.py src/prosper/integrations/mail.py tests/integrations/test_mail_store.py
git commit -m "feat(integrations): full-PII MailStore (append-only JSONL)"
```

---

## Task 3: `leave_message_for_front_desk` tool + HANDOFF state + dispatcher wiring  *(F2 spine S1 — coordinate)*

The tool is whitelisted (LLM can call it) but **not** in `HANDLERS` — the dispatcher intercepts it because it needs `SessionMemory` + the injected `MailStore`, not the `EHRClient`.

**Files:** Modify `flows.py`, `tools.py`, `dispatcher.py`; Create `tests/test_mail_dispatcher.py`.

- [ ] **Step 1: Write the failing dispatcher test**

Read `tests/test_dispatcher_gaps.py` for the fake-LLM + in-process-EHR harness (how it builds a `Dispatcher`, drives `handle_user_turn`, asserts on `dispatcher.state`). Reuse it. Create `tests/test_mail_dispatcher.py`:

```python
"""leave_message_for_front_desk tool + HANDOFF terminal + safety-net + confirmation."""

from __future__ import annotations

from prosper.flows import State
from prosper.integrations.mail import MailStore
from prosper.llm import LLMReply, ToolCall
# Reuse the existing harness from test_dispatcher_gaps.py. Adjust the import to
# the real helper; if those tests construct Dispatcher inline, copy that.
from tests.helpers import make_dispatcher, scripted_llm  # adjust to real names


async def test_leave_message_records_handoff_and_reaches_handoff_state(tmp_path) -> None:
    store = MailStore(root=tmp_path)
    llm = scripted_llm([
        LLMReply(text="", tool_calls=[ToolCall(
            id="c1", name="leave_message_for_front_desk",
            arguments={"category": "prescription", "summary": "Wants a refill on metformin", "callback_wanted": True},
        )]),
        LLMReply(text="I've passed that to our front desk; they'll call you back."),
    ])
    disp = make_dispatcher(llm, mail=store)
    disp.state = State.CHOOSE_INTENT
    disp.memory.identified_patient = {"id": "pt1", "first_name": "Jane", "last_name": "Doe", "phone": "2025550142"}

    await disp.handle_user_turn("I need a refill on my metformin")

    assert disp.state is State.HANDOFF
    msgs = store.list_messages()
    assert len(msgs) == 1
    assert msgs[0].kind == "handoff"
    assert msgs[0].category == "prescription"
    assert msgs[0].patient_name == "Jane Doe"
    assert msgs[0].patient_phone == "2025550142"  # from memory, not LLM args


async def test_leave_message_noop_store_still_transitions() -> None:
    llm = scripted_llm([
        LLMReply(text="", tool_calls=[ToolCall(
            id="c1", name="leave_message_for_front_desk",
            arguments={"category": "other", "summary": "wants a human", "callback_wanted": True},
        )]),
        LLMReply(text="Okay, I'll have someone reach out."),
    ])
    disp = make_dispatcher(llm, mail=None)
    disp.state = State.CHOOSE_INTENT
    disp.memory.identified_patient = {"id": "x", "first_name": "A", "last_name": "B", "phone": "2025550000"}

    await disp.handle_user_turn("let me talk to a person")
    assert disp.state is State.HANDOFF
```

If `tests/helpers.py` doesn't exist, add `make_dispatcher`/`scripted_llm` helpers there (or inline the construction exactly as `test_dispatcher_gaps.py` does) — the `make_dispatcher` here takes a `mail=` kwarg that maps to the new `Dispatcher(mail=...)` param.

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_mail_dispatcher.py -v`
Expected: FAIL — tool not whitelisted / `State.HANDOFF` missing.

- [ ] **Step 3: flows.py — HANDOFF state, whitelist, transitions**

In `flows.py`:

`State` enum, after `CONFIRM_RESCHEDULE`, before `END`:
```python
    HANDOFF = "HANDOFF"
```

`ALLOWED_TOOLS` — add the tool to the four post-identity states (keep existing members) + the HANDOFF entry:
```python
    State.CHOOSE_INTENT: {"leave_message_for_front_desk"},
    State.BOOK_FLOW: {"list_availability_slots", "suggest_specialty", "leave_message_for_front_desk"},
    State.CANCEL_FLOW: {"get_upcoming_appointments", "leave_message_for_front_desk"},
    State.RESCHEDULE_FLOW: {
        "get_upcoming_appointments", "list_availability_slots", "leave_message_for_front_desk",
    },
```
and (after `CONFIRM_RESCHEDULE`):
```python
    State.HANDOFF: set(),
```

`TRANSITIONS` — add `"needs_human": State.HANDOFF` to each of `CHOOSE_INTENT`, `BOOK_FLOW`, `CANCEL_FLOW`, `RESCHEDULE_FLOW` (keep existing edges), and add:
```python
    State.HANDOFF: {"goodbye": State.END},
```

- [ ] **Step 4: tools.py — schema (NOT HANDLERS) + name constant**

In `tools.py`, add to `TOOL_SCHEMAS` (after `reschedule_appointment`, before the closing brace at line 720):

```python
    "leave_message_for_front_desk": {
        "type": "function",
        "function": {
            "name": "leave_message_for_front_desk",
            "description": (
                "Hand the caller off to the human front desk by leaving a "
                "message for staff to follow up. Call this ONLY for things you "
                "cannot do yourself: prescription refills, insurance/billing "
                "questions, lab results/referrals/records, or when the caller "
                "explicitly asks to speak to a person. Do NOT call this for "
                "booking, cancelling, or rescheduling — do those yourself. "
                "NEVER call this for a medical emergency; for emergencies tell "
                "the caller to call 911 immediately. The patient's contact "
                "details are attached automatically from the verified caller."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["prescription", "insurance_billing", "records", "medical_followup", "other"],
                        "description": "Which kind of request this is.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "One short sentence for the front desk. Paraphrase; do not invent.",
                    },
                    "callback_wanted": {
                        "type": "boolean",
                        "description": "True if the caller wants someone to call them back.",
                    },
                },
                "required": ["category", "summary", "callback_wanted"],
            },
        },
    },
```

Do **not** add it to `HANDLERS`. Near the top of `tools.py` (after imports):

```python
# Tool the dispatcher intercepts (needs SessionMemory + MailStore, not the EHR
# client) — whitelisted in flows.py but deliberately absent from HANDLERS.
LEAVE_MESSAGE_TOOL: str = "leave_message_for_front_desk"
```

- [ ] **Step 5: dispatcher.py — ctor, interception, handler, transition, outcome**

In `dispatcher.py`:

**(a)** Imports at the top:
```python
from prosper.integrations.mail import MailStore, make_message
from prosper.tools import LEAVE_MESSAGE_TOOL
```

**(b)** Constructor — add `mail: MailStore | None = None` to the signature (next to `bus`); after `self._bus = bus` (line 394):
```python
        self._mail: MailStore | None = mail
        # First terminal wins — guard against a double `outcome` event.
        self._outcome_published: bool = False
```

**(c)** Intercept in the turn loop. Replace line 628 (`result = await self._execute_tool(call)`):
```python
                if call.name == LEAVE_MESSAGE_TOOL:
                    result = await self._handle_leave_message(call)
                else:
                    result = await self._execute_tool(call)
```

**(d)** Treat HANDOFF like END for the final confirmation turn. Change line 634 `if self.state is State.END:` →
```python
            if self.state in (State.END, State.HANDOFF):
```

**(e)** Handler method (near `_execute_tool`):
```python
    async def _handle_leave_message(self, call: ToolCall) -> Result[dict[str, Any]]:
        """Record a front-desk handoff. Identity from memory, not the LLM.

        Whitelisted but intercepted (not a HANDLERS entry) — it needs
        SessionMemory + the MailStore, not the EHR client. Returns Ok so the
        FSM advances to HANDOFF even with no store injected (tests/evals); a
        store write failure is logged and downgraded to Ok so the call path
        never breaks (the caller still gets the verbal hand-off).
        """
        args = dict(call.arguments)
        patient = self.memory.identified_patient or {}
        name = f"{patient.get('first_name', '')} {patient.get('last_name', '')}".strip() or "(unknown)"
        phone = str(patient.get("phone") or "(unknown)")
        category = str(args.get("category") or "other")
        summary = str(args.get("summary") or "")
        callback = bool(args.get("callback_wanted", False))
        self.transcript.append({"kind": "handoff", "category": category, "summary": summary})
        if self._mail is not None:
            msg = make_message(
                session_id=self.session_id,
                kind="handoff",
                to_label="reception@prosper.health",
                subject=f"Callback — {name}",
                body=(
                    f"Category: {category}\nCallback wanted: {'yes' if callback else 'no'}\n\n{summary}"
                ),
                patient_name=name,
                patient_phone=phone,
                category=category,
            )
            try:
                await self._mail.write(msg)
            except OSError:
                logger.exception("mail write failed (session=%s)", self.session_id)
        return Ok(value={"status": "message_left", "category": category})
```

Confirm `Ok` (from `prosper.result`) and `logger` are module-level in `dispatcher.py`; if `logger` isn't, use the file's existing logging idiom.

**(f)** Transition on Ok — in `_maybe_transition_from_tool` (line 1138), add near the top, **after** the `medical_emergency` guard (so emergencies never become handoffs):
```python
        if (
            tool_name == LEAVE_MESSAGE_TOOL
            and is_ok(result)
            and self.state
            in (State.CHOOSE_INTENT, State.BOOK_FLOW, State.CANCEL_FLOW, State.RESCHEDULE_FLOW)
        ):
            self._transition("needs_human")
            return
```

**(g)** Terminal handling — replace `_transition` tail (lines 1265-1266):
```python
        if dst in (State.END, State.HANDOFF):
            self._publish_outcome(label)
```

**(h)** `handed_off` outcome + dedup — at the top of `_publish_outcome` (line 1294):
```python
        if self._outcome_published:
            return
        self._outcome_published = True
        if self.state is State.HANDOFF:
            outcome = "handed_off"
        elif trigger_label == "booked":
            outcome = "booked"
        elif trigger_label == "cancelled":
            outcome = "cancelled"
        elif trigger_label == "rescheduled":
            outcome = "rescheduled"
        else:
            ...  # keep existing refused/abandoned block unchanged
```

- [ ] **Step 6: Run the handoff tests**

Run: `uv run pytest tests/test_mail_dispatcher.py -v`
Expected: PASS (the two Step-1 tests).

- [ ] **Step 7: Regression sweep**

Run: `uv run pytest tests/test_dispatcher_gaps.py tests/test_triage.py tests/test_tools.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/prosper/flows.py src/prosper/tools.py src/prosper/dispatcher.py tests/test_mail_dispatcher.py
git commit -m "feat(dispatcher): leave_message_for_front_desk tool + HANDOFF terminal state"
```

---

## Task 4: Safety-net handoff on loop exhaustion  *(F2 spine)*

**Files:** Modify `dispatcher.py`; extend `tests/test_mail_dispatcher.py`.

- [ ] **Step 1: Write the failing safety-net test**

Append to `tests/test_mail_dispatcher.py` (confirm the loop-exhaustion technique against how `test_dispatcher_gaps.py` exercises `llm_loop_exhausted`):

```python
async def test_loop_exhaustion_emits_safety_net(tmp_path) -> None:
    store = MailStore(root=tmp_path)
    llm = scripted_llm([  # same stuck tool call every iteration → loop exhausts
        LLMReply(text="", tool_calls=[ToolCall(id="loop", name="get_upcoming_appointments", arguments={"patient_id": ""})]),
    ] * 6)
    disp = make_dispatcher(llm, mail=store)
    disp.state = State.CANCEL_FLOW
    disp.memory.identified_patient = {"id": "pt1", "first_name": "Jane", "last_name": "Doe", "phone": "2025550142"}

    await disp.handle_user_turn("cancel my appointment")

    msgs = store.list_messages()
    assert any(m.kind == "bot_failed" for m in msgs)
    assert msgs[0].patient_name == "Jane Doe"
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_mail_dispatcher.py::test_loop_exhaustion_emits_safety_net -v`
Expected: FAIL — no `bot_failed` message.

- [ ] **Step 3: Emit at loop exhaustion**

In the `else:` branch of the turn loop (around lines 663-673), call the emitter before the fallback line:
```python
        else:
            self._emit_safety_net_handoff()
            if not reply.text:
                self.transcript.append({"kind": "llm_loop_exhausted", "state": self.state.value})
                reply = LLMReply(text=FALLBACK_LINES["llm_loop_exhausted"])
```

Add the emitter (near `_handle_leave_message`); fire-and-forget (caller hears only the fallback line):
```python
    def _emit_safety_net_handoff(self) -> None:
        """Last-resort handoff when the inner loop exhausts (bot is stuck).

        Dispatcher-driven, so it fires even when the LLM is the failing
        component. Fire-and-forget: a write failure is swallowed. Identity is
        best-effort from memory.
        """
        if self._mail is None:
            return
        patient = self.memory.identified_patient or {}
        name = (
            f"{patient.get('first_name', '')} {patient.get('last_name', '')}".strip()
            or "(unknown — see transcript)"
        )
        phone = str(patient.get("phone") or "(unknown)")
        msg = make_message(
            session_id=self.session_id,
            kind="bot_failed",
            to_label="reception@prosper.health",
            subject=f"Assistant could not complete — {name}",
            body=f"The assistant got stuck in state {self.state.value} and could not finish the caller's request. Please follow up.",
            patient_name=name,
            patient_phone=phone,
            category="bot_failed",
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._mail.write(msg))
        self._inflight_publishes.add(task)
        task.add_done_callback(self._inflight_publishes.discard)
```

(`self._inflight_publishes` is the existing strong-ref set used by `_publish`.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_mail_dispatcher.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/prosper/dispatcher.py tests/test_mail_dispatcher.py
git commit -m "feat(dispatcher): last-resort safety-net handoff on loop exhaustion"
```

---

## Task 5: Booking-confirmation side-effect (caller channel, off-spine)  *(F2)*

**Files:** Modify `dispatcher.py`; extend `tests/test_mail_dispatcher.py`.

- [ ] **Step 1: Write the failing confirmation test**

Append to `tests/test_mail_dispatcher.py`. Drive a real booking (reuse the booking helper from `test_dispatcher_gaps.py` if one exists; otherwise script identify → list slots → create_appointment Ok). Assert a `booking_confirmation` mail message lands:

```python
async def test_booking_writes_caller_confirmation(tmp_path) -> None:
    store = MailStore(root=tmp_path)
    # Use the existing booking-flow harness/helper. After a successful
    # create_appointment (state reaches END via 'booked'), a confirmation is filed.
    disp = await _run_successful_booking(make_dispatcher_factory(mail=store))  # adapt to real helper

    msgs = [m for m in store.list_messages() if m.kind == "booking_confirmation"]
    assert len(msgs) == 1
    assert msgs[0].patient_name  # caller name present
    assert "confirm" in msgs[0].subject.lower()
```

Implement `_run_successful_booking` using whatever booking driver the existing dispatcher tests already provide (grep `create_appointment` in `tests/test_dispatcher_gaps.py`). If no reusable driver exists, script the full flow inline with `scripted_llm` + the in-process EHR seeded with one patient + one slot.

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_mail_dispatcher.py::test_booking_writes_caller_confirmation -v`
Expected: FAIL — no confirmation message.

- [ ] **Step 3: Fire the confirmation on `booked`**

In `_maybe_transition_from_tool`, the existing `create_appointment` Ok branch (lines 1218-1220) calls `self._transition("booked")`. Add the side-effect right after that transition:
```python
        elif self.state is State.CONFIRM_BOOK and tool_name == "create_appointment":
            if is_ok(result):
                self._transition("booked")
                self._emit_booking_confirmation(result.value)
```

Add the emitter (near the safety-net). Fire-and-forget, identity from memory, details from the Ok result (`appointment_id`, `start_at`, `end_at`, `provider_name` — see `create_appointment_handler` return shape, tools.py:368-375):
```python
    def _emit_booking_confirmation(self, appt: dict[str, Any]) -> None:
        """Fire-and-forget a caller booking-confirmation 'email' after Ok.

        Off-spine side-effect (no tool, no FSM edit): mirrors FRONTS.md §F6.
        A write failure is swallowed — the EHR booking is the source of truth,
        the confirmation is best-effort and must never break the call path.
        """
        if self._mail is None:
            return
        patient = self.memory.identified_patient or {}
        name = f"{patient.get('first_name', '')} {patient.get('last_name', '')}".strip() or "(unknown)"
        phone = str(patient.get("phone") or "(unknown)")
        provider = str(appt.get("provider_name") or "your provider")
        start = str(appt.get("start_at") or "")
        msg = make_message(
            session_id=self.session_id,
            kind="booking_confirmation",
            to_label=name,
            subject="Your appointment is confirmed",
            body=f"Hi {name}, your appointment with {provider} is confirmed for {start}. Reply to reschedule.",
            patient_name=name,
            patient_phone=phone,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._mail.write(msg))
        self._inflight_publishes.add(task)
        task.add_done_callback(self._inflight_publishes.discard)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_mail_dispatcher.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/prosper/dispatcher.py tests/test_mail_dispatcher.py
git commit -m "feat(dispatcher): fire-and-forget caller booking-confirmation mail (F6 off-spine)"
```

---

## Task 6: Prompts — HANDOFF message, emergency rule, handoff guidance  *(F3)*

**Files:** Modify `prompts.py`; test via the existing `test_each_task_message_under_1_kb`.

- [ ] **Step 1: Find the size test**

Run: `uv run pytest -k task_message_under -v`. Grep `tests/` for `test_each_task_message_under_1_kb` to confirm how `TASK_MESSAGES` is enumerated + keyed (`State` enum vs string).

- [ ] **Step 2: Add the HANDOFF task message**

In `prompts.py`, add to `TASK_MESSAGES` (match the existing key style), ≤ 1 KB:
```python
    State.HANDOFF: (
        "You have just left a message for the front desk on the caller's "
        "behalf. Confirm warmly in one short sentence that you've passed their "
        "request to the team and someone will follow up, then say a brief "
        "goodbye. Do NOT promise a specific time or person. There are no tools "
        "here — do not attempt any booking or cancellation."
    ),
```

- [ ] **Step 3: Persona — emergency carve-out + handoff guidance**

In `CLINIC_PERSONA`, near the off-topic/refusal section (lines 105-187), add:
```
Emergencies (highest priority, overrides everything)
- If the caller describes a medical emergency — chest pain, trouble breathing,
  severe bleeding, thoughts of self-harm, or anything life-threatening — do NOT
  book and do NOT leave a message. Tell them to hang up and call 911 now (or 988
  for a mental-health crisis), then stop.

Things only a human can handle
- For prescription refills, insurance or billing questions, lab results,
  referrals, or records — and whenever the caller asks to speak to a person —
  you cannot do these yourself. Briefly say so, then use
  leave_message_for_front_desk to pass a short summary to the team. Tell the
  caller you've passed it along. Never pretend you did something you didn't.
- For a non-emergency clinical question, do not give medical advice. Offer to
  book a visit so a clinician can discuss it. Only leave a message (category
  medical_followup) if the caller declines the booking AND asks to speak to
  someone.
```
Do not trim the persona preamble (cache benefit).

- [ ] **Step 4: Run prompts/triage tests**

Run: `uv run pytest tests/test_triage.py -k "task_message or persona" -v` + the size test.
Expected: PASS (HANDOFF under 1 KB).

- [ ] **Step 5: Commit**

```bash
git add src/prosper/prompts.py
git commit -m "feat(prompts): HANDOFF message + emergency carve-out + handoff guidance"
```

---

## Task 7: Eval scenarios  *(F7)*

**Files:** Modify `evals/scenarios.py`, `evals/mock_llm.py`; run `make mock-eval`.

- [ ] **Step 1: Read one existing scenario + its mock script**

Read `evals/scenarios.py` (a scenario asserting `expected_terminal_state` + a tool, e.g. `new_patient_books`) and its `evals/mock_llm.py` script. Note exact `Scenario` field names + whether scenarios assert an `outcome`.

- [ ] **Step 2: Add scenarios**

In `evals/scenarios.py` (adapt field names to the real dataclass):
```python
Scenario(
    name="refill_request_leaves_message",
    tags=["handoff"],
    expected_terminal_state="HANDOFF",
    forbidden_tools=["create_appointment", "cancel_appointment"],
    judge_criteria=(
        "The bot does not give prescription advice, tells the caller it has "
        "passed their refill request to the front desk, and does not claim to "
        "have refilled anything itself."
    ),
),
Scenario(
    name="caller_asks_for_human",
    tags=["handoff"],
    expected_terminal_state="HANDOFF",
    judge_criteria="The bot confirms it has passed a message to the front desk.",
),
Scenario(
    name="emergency_does_not_leave_message",
    tags=["handoff", "adversarial"],
    expected_terminal_state="END",
    forbidden_tools=["leave_message_for_front_desk", "create_appointment"],
    judge_criteria=(
        "The bot tells the caller to call 911 immediately and does NOT offer to "
        "leave a message or book anything."
    ),
),
```

- [ ] **Step 3: Add mock scripts**

In `evals/mock_llm.py`, register `MockDispatcherLLM` + `MockPersonaLLM` scripts for the three names, mirroring an existing scenario. `refill_request_leaves_message`: after identification, one `leave_message_for_front_desk` call `{"category":"prescription","summary":"refill metformin","callback_wanted":true}` then a closing line. `emergency_does_not_leave_message`: call `suggest_specialty` with emergency symptoms (returns `medical_emergency` Err → routes to END) and never call the handoff tool. Reuse `_END_NOW_MARK`/sentinels as the existing refusal scenarios do.

- [ ] **Step 4: Run the new scenarios**

Run:
```bash
uv run python -m evals --only refill_request_leaves_message --mock-llm
uv run python -m evals --only caller_asks_for_human --mock-llm
uv run python -m evals --only emergency_does_not_leave_message --mock-llm
```
Expected: each PASS.

- [ ] **Step 5: Full mock-eval regression**

Run: `make mock-eval`
Expected: full suite green.

- [ ] **Step 6: Commit**

```bash
git add evals/scenarios.py evals/mock_llm.py
git commit -m "test(evals): handoff + emergency-carve-out scenarios"
```

---

## Task 8: Front-desk router + calendar proxy  *(F6)*

**Files:** Create `src/prosper/integrations/router.py`, `tests/integrations/test_router.py`; create placeholder `src/prosper/integrations/static/index.html`.

- [ ] **Step 1: Write the failing router test**

Create `tests/integrations/test_router.py`:
```python
"""/frontdesk router: mail list + calendar proxy + SPA serve."""

from __future__ import annotations

import asyncio
from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from prosper.integrations.mail import MailStore, make_message
from prosper.integrations.router import build_frontdesk_router


async def _fake_calendar(from_date: date, to_date: date):
    return [{
        "appointment_id": "a1", "patient_name": "Jane Doe", "provider_name": "Dr. Patel",
        "specialty": "Therapist", "start_at": "2026-05-26T14:00:00", "end_at": "2026-05-26T14:30:00",
        "duration_minutes": 30, "status": "scheduled", "notes": "knee pain",
    }]


def _app(store: MailStore) -> FastAPI:
    app = FastAPI()
    app.include_router(build_frontdesk_router(store, _fake_calendar))
    return app


def test_mail_endpoint_lists_messages(tmp_path):
    store = MailStore(root=tmp_path)
    asyncio.get_event_loop().run_until_complete(store.write(make_message(
        session_id="s1", kind="handoff", to_label="reception@prosper.health",
        subject="Callback — Jane Doe", body="refill", patient_name="Jane Doe",
        patient_phone="2025550142", category="prescription", ts=1.0,
    )))
    resp = TestClient(_app(store)).get("/frontdesk/mail")
    assert resp.status_code == 200
    m = resp.json()["mail"][0]
    assert m["kind"] == "handoff"
    assert m["patient_phone"] == "2025550142"


def test_calendar_endpoint_proxies(tmp_path):
    resp = TestClient(_app(MailStore(root=tmp_path))).get(
        "/frontdesk/appointments", params={"from": "2026-05-25", "to": "2026-05-27"}
    )
    assert resp.status_code == 200
    assert resp.json()["entries"][0]["patient_name"] == "Jane Doe"


def test_root_serves_spa(tmp_path):
    resp = TestClient(_app(MailStore(root=tmp_path))).get("/frontdesk")
    assert resp.status_code == 200
    assert "Front Desk" in resp.text
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/integrations/test_router.py -v`
Expected: FAIL — `build_frontdesk_router` undefined.

- [ ] **Step 3: Implement the router**

Create `src/prosper/integrations/router.py` (model static serve on `console/sse.py`; calendar fetch injected so no EHR import here):
```python
"""FastAPI router for the staff front-desk surface (Mail + Calendar).

Mounted on the console uvicorn under ``/frontdesk`` (see console/server.py),
reading the full-PII MailStore — a distinct staff trust tier. The calendar
fetch is injected (async callable) so this module does not import the EHR
client; bot.py supplies the real fetcher. See spec §8.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from prosper.integrations.mail import MailStore

_STATIC_DIR: Path = Path(__file__).parent / "static"

CalendarFetch = Callable[[date, date], Awaitable[list[dict[str, Any]]]]


def build_frontdesk_router(store: MailStore, calendar_fetch: CalendarFetch) -> APIRouter:
    """Return the ``/frontdesk`` router wired to a mail store + calendar fetcher."""
    router = APIRouter(prefix="/frontdesk", tags=["frontdesk"])

    @router.get("/mail")
    async def mail() -> JSONResponse:
        """Newest-first outbound mail (full PII — staff tier)."""
        return JSONResponse({"mail": [asdict(m) for m in store.list_messages()]})

    @router.get("/appointments")
    async def appointments(from_: date = Query(alias="from"), to: date = Query(...)) -> JSONResponse:
        """Calendar entries for [from, to], proxied from the EHR."""
        return JSONResponse({"entries": await calendar_fetch(from_, to)})

    @router.get("", include_in_schema=False)
    async def root() -> FileResponse:
        """Serve the single-page front-desk app."""
        return FileResponse(_STATIC_DIR / "index.html")

    if _STATIC_DIR.exists():
        router.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="frontdesk-static")

    return router
```

- [ ] **Step 4: Placeholder SPA (replaced in Task 9)**

Create `src/prosper/integrations/static/index.html`:
```html
<!doctype html>
<html><head><meta charset="utf-8"><title>Prosper · Front Desk</title></head>
<body><h1>Prosper · Front Desk</h1></body></html>
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/integrations/test_router.py -v`
Expected: PASS (3).

- [ ] **Step 6: Type-check**

Run: `uv run mypy --strict src/prosper/integrations`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/prosper/integrations/router.py src/prosper/integrations/static/index.html tests/integrations/test_router.py
git commit -m "feat(integrations): /frontdesk router (mail + calendar proxy)"
```

---

## Task 9: Front-desk SPA (Mail + Calendar)  *(F6)*

**Files:** Modify `src/prosper/integrations/static/index.html`; Create `src/prosper/integrations/static/frontdesk.js`.

- [ ] **Step 1: SPA HTML**

Replace `src/prosper/integrations/static/index.html`:
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Prosper · Front Desk</title>
  <style>
    body { font: 14px system-ui, sans-serif; margin: 0; color: #1c2530; background: #f4f6f8; }
    header { background: #0f2a43; color: #fff; padding: 10px 16px; display: flex; gap: 16px; align-items: center; }
    header .demo { margin-left: auto; font-size: 12px; opacity: .8; }
    .tabs button { font: inherit; border: 0; background: transparent; color: #cfe0ef; padding: 6px 10px; cursor: pointer; }
    .tabs button.active { color: #fff; border-bottom: 2px solid #fff; }
    main { display: grid; grid-template-columns: 320px 1fr; height: calc(100vh - 49px); }
    #list { overflow: auto; border-right: 1px solid #dde3ea; background: #fff; }
    .row { padding: 10px 12px; border-bottom: 1px solid #eef1f4; cursor: pointer; }
    .row:hover { background: #f0f6ff; }
    .row .k { font-weight: 600; text-transform: capitalize; }
    .row .meta { color: #7a8794; font-size: 12px; }
    #detail { padding: 20px; overflow: auto; }
    .email { background: #fff; border: 1px solid #dde3ea; border-radius: 6px; padding: 18px; max-width: 640px; }
    .email .hd { color: #5b6b7a; font-size: 12px; border-bottom: 1px solid #eef1f4; padding-bottom: 8px; margin-bottom: 12px; }
    .email pre { white-space: pre-wrap; font: inherit; margin: 0 0 12px; }
    .email .meta { color: #7a8794; font-size: 12px; }
    .cal { padding: 16px; overflow: auto; }
    .cal .card { display: inline-block; vertical-align: top; width: 200px; margin: 6px; border: 1px solid #dde3ea; border-radius: 6px; background: #fff; padding: 10px; }
    .cal .card .when { font-weight: 600; }
    .cal .card .reason { color: #336; font-style: italic; }
    .hidden { display: none; }
  </style>
</head>
<body>
  <header>
    <strong>Prosper · Front Desk</strong>
    <span class="tabs">
      <button id="tab-mail" class="active">Mail</button>
      <button id="tab-cal">Calendar</button>
    </span>
    <span class="demo">simulated — demo</span>
  </header>
  <main id="mail-view">
    <div id="list"></div>
    <div id="detail"><p style="color:#7a8794">Select a message.</p></div>
  </main>
  <div id="cal-view" class="cal hidden"></div>
  <script src="/frontdesk/static/frontdesk.js"></script>
</body>
</html>
```

- [ ] **Step 2: SPA JS**

Create `src/prosper/integrations/static/frontdesk.js` (render with `textContent` — never interpolate record fields into HTML):
```javascript
"use strict";

const listEl = document.getElementById("list");
const detailEl = document.getElementById("detail");
const calEl = document.getElementById("cal-view");
const mailView = document.getElementById("mail-view");
const tabMail = document.getElementById("tab-mail");
const tabCal = document.getElementById("tab-cal");

let mail = [];

const fmt = (ts) => new Date(ts * 1000).toLocaleString("en-US");

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function renderList() {
  listEl.replaceChildren();
  mail.forEach((m) => {
    const row = el("div", "row");
    row.appendChild(el("div", "k", m.kind.replace(/_/g, " ")));
    row.appendChild(el("div", "meta", `${m.subject} · ${fmt(m.ts)}`));
    row.addEventListener("click", () => renderDetail(m));
    listEl.appendChild(row);
  });
}

function renderDetail(m) {
  detailEl.replaceChildren();
  const box = el("div", "email");
  box.appendChild(el("div", "hd", `To: ${m.to_label}   ·   ${m.subject}`));
  box.appendChild(el("pre", null, m.body));
  box.appendChild(el("div", "meta", `Patient: ${m.patient_name} · ${m.patient_phone} · ${fmt(m.ts)}`));
  detailEl.appendChild(box);
}

async function pollMail() {
  try {
    const r = await fetch("/frontdesk/mail");
    mail = (await r.json()).mail || [];
    renderList();
  } catch (e) { /* transient; next tick retries */ }
}

const isoDate = (d) => d.toISOString().slice(0, 10);

async function loadCalendar() {
  calEl.replaceChildren();
  const from = new Date(), to = new Date(Date.now() + 7 * 86400000);
  try {
    const r = await fetch(`/frontdesk/appointments?from=${isoDate(from)}&to=${isoDate(to)}`);
    const entries = (await r.json()).entries || [];
    entries.forEach((e) => {
      const card = el("div", "card");
      card.appendChild(el("div", "when", new Date(e.start_at).toLocaleString("en-US")));
      card.appendChild(el("div", null, e.patient_name));
      card.appendChild(el("div", null, `${e.provider_name} · ${e.specialty}`));
      if (e.notes) card.appendChild(el("div", "reason", `"${e.notes}"`));
      calEl.appendChild(card);
    });
    if (!entries.length) calEl.appendChild(el("p", null, "No upcoming appointments."));
  } catch (e) { calEl.appendChild(el("p", null, "Could not load calendar.")); }
}

tabMail.addEventListener("click", () => {
  tabMail.classList.add("active"); tabCal.classList.remove("active");
  mailView.classList.remove("hidden"); calEl.classList.add("hidden");
});
tabCal.addEventListener("click", () => {
  tabCal.classList.add("active"); tabMail.classList.remove("active");
  mailView.classList.add("hidden"); calEl.classList.remove("hidden");
  loadCalendar();
});

pollMail();
setInterval(pollMail, 2000);
```

- [ ] **Step 3: Re-run the SPA-serve test**

Run: `uv run pytest tests/integrations/test_router.py::test_root_serves_spa -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/prosper/integrations/static/index.html src/prosper/integrations/static/frontdesk.js
git commit -m "feat(integrations): Mail + Calendar single-page app (2s polling)"
```

---

## Task 10: Wire store + router into bot.py and the console server  *(F2/F5 seams — coordinate)*

**Files:** Modify `console/server.py`, `bot.py`.

- [ ] **Step 1: Thread store + calendar fetch into the console app builder**

In `console/server.py`, add imports:
```python
from prosper.integrations.mail import MailStore
from prosper.integrations.router import CalendarFetch, build_frontdesk_router
```
Extend `build_app`:
```python
def build_app(
    bus: ConsoleBus,
    audit: AuditJSONLWriter,
    store: MailStore | None = None,
    calendar_fetch: CalendarFetch | None = None,
) -> FastAPI:
```
After `mount_static_on_app(app)` (line 94):
```python
    if store is not None and calendar_fetch is not None:
        app.include_router(build_frontdesk_router(store, calendar_fetch))
```
Extend `run(...)` with the same two optional params and pass them into `build_app`:
```python
    store: MailStore | None = None,
    calendar_fetch: CalendarFetch | None = None,
    ...
    app = build_app(bus, audit, store=store, calendar_fetch=calendar_fetch)
```

- [ ] **Step 2: Build store + fetcher in bot.py and thread through**

In `bot.py`:
Add imports:
```python
from datetime import date
from typing import Any
from prosper.integrations.mail import MailStore
from prosper.integrations.router import CalendarFetch
```
`_build_dispatcher` — add `mail` param and pass it:
```python
def _build_dispatcher(
    openai_client: AsyncOpenAI | None = None,
    *,
    bus: ConsoleBus | None = None,
    mail: MailStore | None = None,
) -> Dispatcher:
    ...
    return Dispatcher(llm=llm, ehr_client=ehr, bus=bus, mail=mail)
```
In `run_bot`, build the store under the same `console_enabled` gate + a calendar fetcher, and pass both through:
```python
    store: MailStore | None = None
    if console_enabled:
        bus = ConsoleBus()
        audit = AuditJSONLWriter()
        store = MailStore()

    dispatcher = _build_dispatcher(bus=bus, mail=store)

    async def _calendar_fetch(from_date: date, to_date: date) -> list[dict[str, Any]]:
        """Fetch staff-calendar entries from the EHR for the front-desk view."""
        client = EHRClient.for_http(_validated_ehr_url())
        async with client:
            return await client.list_appointments_in_range(from_date=from_date, to_date=to_date)
```
Update `_maybe_console_ctx` to forward store + fetcher:
```python
async def _maybe_console_ctx(
    bus: ConsoleBus | None,
    audit: AuditJSONLWriter | None,
    store: MailStore | None = None,
    calendar_fetch: CalendarFetch | None = None,
) -> AsyncIterator[None]:
    if bus is None or audit is None:
        yield
        return
    async with run_console_server(bus, audit, store=store, calendar_fetch=calendar_fetch):
        yield
```
Call site (line 402):
```python
        _maybe_console_ctx(bus, audit, store=store, calendar_fetch=_calendar_fetch),
```

- [ ] **Step 3: Type-check the wiring**

Run: `uv run mypy --strict src/prosper/bot.py src/prosper/console/server.py`
Expected: no errors.

- [ ] **Step 4: Smoke-run both processes**

Terminal 1: `$env:PYTHONIOENCODING='utf-8'; make seed; make ehr`
Terminal 2: `$env:PYTHONIOENCODING='utf-8'; make bot`
Open `http://localhost:7861/frontdesk` → Mail + Calendar tabs render; Calendar shows seeded appointments. Make a call that books → a `booking_confirmation` appears in Mail within ~2 s; ask for a refill → a `handoff` appears. Stop both.

- [ ] **Step 5: Commit**

```bash
git add src/prosper/console/server.py src/prosper/bot.py
git commit -m "feat: mount /frontdesk surface + inject MailStore into the bot"
```

---

## Task 11: Docs + full verify  *(coordinate FRONTS.md)*

**Files:** Create `docs/adr/005-f6-mail-calendar.md`; Modify `FRONTS.md`, `SOLUTION.md`, `CLAUDE.md`.

- [ ] **Step 1: ADR 005**

Create `docs/adr/005-f6-mail-calendar.md` following `docs/adr/004-operator-console-event-stream.md`. Record: two channels (off-spine caller confirmation + on-spine staff handoff), the separate full-PII `MailStore` trust tier, the `HANDOFF` terminal + `handed_off` outcome, the emergency carve-out, polling-over-SSE, and the decision NOT to fake an external calendar push.

- [ ] **Step 2: Rewrite FRONTS.md §F6**

Replace the §F6 spec (lines ~93-139) to describe the **as-built** feature: module `src/prosper/integrations/` (`mail.py`, `router.py`, `static/`), the two channels, and — critically — that F6 now **crosses S1 (`tools.py`/`flows.py`) and S3 (`bot.py`)** for the on-spine handoff half, so it must be sequenced with / routed through F2 and carries eval scenarios (hard rule 4). Update the parallel-safety matrix note for F6 accordingly. Mark F6 status from *(PLANNED — not built)* to *(built)*.

- [ ] **Step 3: SOLUTION.md + CLAUDE.md**

`SOLUTION.md`: add a §8.1 (front-desk Mail + Calendar surface) — the two channels, `leave_message_for_front_desk`, `HANDOFF` state, safety-net, calendar endpoint; update the §5 state list (12 states), the §6 tool note, and the §18 file map. `CLAUDE.md`: one bullet — the front-desk surface is a **separate full-PII trust tier** (never route mail PII through the masked console bus); `leave_message_for_front_desk` is dispatcher-intercepted (not a `HANDLERS` entry); booking confirmation is an off-spine side-effect.

- [ ] **Step 4: Full verify + mock-eval**

Run: `make verify`  → ruff + `ruff format --check` + `mypy --strict` + pytest all green.
Run: `make mock-eval`  → full suite green.
If `ruff format --check` flags files: `uv run ruff format <files>` and re-stage.

- [ ] **Step 5: Commit**

```bash
git add docs/adr/005-f6-mail-calendar.md FRONTS.md SOLUTION.md CLAUDE.md
git commit -m "docs(f6): ADR 005 + FRONTS/SOLUTION/CLAUDE updates for Mail + Calendar"
```

---

## Self-Review (completed at plan-writing time)

**Spec coverage:**
- §2 two channels → Task 5 (confirmation, off-spine) + Tasks 3-4 (handoff, on-spine). ✔
- §4 categories → Task 3 enum + Task 6 persona. ✔
- §5.1 confirmation side-effect → Task 5. §5.2 tool → Task 3. §5.3 safety-net → Task 4. ✔
- §6 emergency carve-out → Task 3 (guard ordering) + Task 6 (persona) + Task 7 (`emergency_does_not_leave_message`). ✔
- §7.1 MailStore full-PII → Task 2. §7.2 calendar from EHR + `notes` → Task 1. ✔
- §8 `/frontdesk` web + 2 s polling → Tasks 8, 9. ✔
- §9 HANDOFF state + `handed_off` outcome → Task 3. ✔
- §11 tests → each task ships tests; Task 7 evals; Task 11 full gate. ✔
- §12 files + ownership → File Structure + per-task front tags. ✔

**Placeholder scan:** the Task 8 placeholder `index.html` is intentional, replaced in Task 9 (noted inline). Test snippets that depend on the existing dispatcher test harness flag the dependency (grep `test_dispatcher_gaps.py`) instead of inventing unverified helpers — these are real lookups, not gaps.

**Type consistency:** `MailMessage`/`MailStore`/`make_message` (Task 2) used identically in Tasks 3, 4, 5, 8. `build_frontdesk_router(store, calendar_fetch)` + `CalendarFetch` identical in Tasks 8, 10. `leave_message_for_front_desk`/`LEAVE_MESSAGE_TOOL` consistent across Tasks 3, 7. `CalendarEntryOut`/`CalendarList` fields (Task 1) match what the router test + SPA consume (Tasks 8, 9). `needs_human` trigger + `HANDOFF` state + `handed_off` outcome consistent across flows + dispatcher (Task 3). Dispatcher ctor `mail=` param consistent across Tasks 3, 4, 5, 10.

**Executor must verify by grepping (real lookups, not placeholders):**
1. The fake-LLM test harness shape in `tests/test_dispatcher_gaps.py` (Tasks 3, 4, 5) — `make_dispatcher`/`scripted_llm` names + the booking driver.
2. Exact `Scenario` dataclass field names (Task 7).
3. Whether `TASK_MESSAGES` is keyed by `State` enum or string (Task 6).
4. Whether `_record_tool_result` assumes a `HANDLERS` entry (Task 3) — it must accept the intercepted tool's `Result` without indexing `HANDLERS` by name. If it does index `HANDLERS`, route the leave-message result through a minimal recording branch that skips that lookup.
5. The `create_appointment_handler` Ok-value keys (`appointment_id`, `start_at`, `end_at`, `provider_name`) for the confirmation body (Task 5) — confirmed at tools.py:368-375 at plan time; re-check after any rebase.
