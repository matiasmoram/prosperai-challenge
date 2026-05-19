# Prosper Health Challenge — Design Spec (v2)

**Author:** Matías (with LLM council deliberations)
**Date:** 2026-05-19 (v1) → 2026-05-20 (v2: cross-survey integration)
**Ambition:** Plan B — solid core + two well-executed bonuses
**Bonuses:** Automated eval suite (LLM-as-judge + state assertion) + latency handling

**v2 changelog (council 5):** added pre-seeded `Slot` model, declarative
`Scenario` dataclass with paired state-assertion, long stable `CLINIC_PERSONA`
preamble for prompt-cache hits, `Result[Ok, Err]` typed integration returns,
`CLAUDE.md` + pre-commit + minimal GH Actions, `SOLUTION.md` real-transcript +
dev-log + measured latency table, optional HeadlessFlow eval runner and
`--baseline` regression diff. Skipped: layout refactor, verify-don't-retry
(SQLite loopback eliminates write-timeout risk), pre-recorded TTS fallback.

---

## 1. Goal

Build a voice agent that schedules appointments at a health clinic. Pipecat
pipeline (ElevenLabs STT/TTS + OpenAI LLM) drives the conversation; agent talks
to an Electronic Health Record (EHR) HTTP API to create/find patients and
book/cancel appointments. EHR persists data across restarts.

Deliverables:
- 5 HTTP EHR endpoints with persistent DB
- Conversation flow that identifies new vs existing patient, registers if new,
  books or cancels appointments
- Integration via LLM tool calling
- `SOLUTION.md` with decisions and trade-offs
- Automated eval suite (LLM-as-judge over scripted scenarios + smoke audio tests)
- Latency instrumentation + tight prompts (+ optional availability cache)

---

## 2. Architectural decisions

Every numbered decision below was deliberated by a three-model LLM council
(kimi-k2-0905 + gemini-2.5-pro + gpt-5-mini, chaired by claude/sonnet). The
verdict line summarises consensus.

### 2.1 Conversation orchestration: **Hybrid FSM**

A lightweight explicit finite state machine controls high-level flow.
Inside each state the LLM has freedom but only a whitelisted subset of tools.

**Council verdict:** unanimous Option 3 (Hybrid) over single mega-prompt or pure
FSM. Reasoning: mega-prompt is opaque to evals and hallucinates over long
contexts; pure FSM feels robotic. Hybrid gives deterministic state transitions
(easy to assert in evals) plus natural in-state language.

States:

```
GREETING
    ↓
IDENTIFY_PATIENT  ──(no match)──▶  REGISTER_PATIENT
    ↓ (found)                              ↓
CHOOSE_INTENT  ◀───────────────────────────┘
    ↓
BOOK_FLOW   or   CANCEL_FLOW
    ↓                ↓
CONFIRM (write)  CONFIRM (write)
    ↓                ↓
END             END
```

Per-state tool whitelist:

| State              | Allowed tools                                    |
|--------------------|--------------------------------------------------|
| `GREETING`         | none                                             |
| `IDENTIFY_PATIENT` | `find_patient_by_phone`, `find_patient_by_name_dob` |
| `REGISTER_PATIENT` | `create_patient` (write, confirm-gated)          |
| `CHOOSE_INTENT`    | none (intent detection only)                     |
| `BOOK_FLOW`        | `list_availability_slots`                        |
| `CANCEL_FLOW`      | (uses appointments returned by find_patient)     |
| `CONFIRM`          | `create_appointment` or `cancel_appointment`     |
| `END`              | none                                             |

Tool whitelist is enforced at the dispatcher level (deny any tool call not in
the active state's list), not by prompt instructions alone.

### 2.2 FSM library: **Custom lightweight dispatcher**

**Council verdict:** Custom dispatcher (~100 LOC) inside Pipecat pipeline over
Pipecat Flows official package or Option 1 mega-prompt.

Reasoning: Pipecat Flows adds a separate package, NodeConfig boilerplate, and
extra indirection on a latency-sensitive path. Reviewers prefer a single
auditable file (`flows.py`) over plumbing through an unfamiliar abstraction.
Designed so migration to Pipecat Flows later is a mechanical change.

Mechanism: dispatcher swaps `LLMContext.system_message` and the `tools` list
passed to `OpenAILLMService` on each state transition. State stored in a small
`SessionState` object held in module scope keyed by client/session id.

### 2.3 EHR process topology: **Separate FastAPI process, httpx calls**

**Council verdict:** unanimous Option (a) — EHR runs as its own FastAPI process,
bot calls it via httpx.

Reasoning: mirrors real-world EHR integration (HL7/FHIR), reviewers can stress
the EHR independently, the HTTP contract is the testable boundary. Local
HTTP overhead (~1 ms) is irrelevant vs the STT/LLM/TTS latency budget.

Implementation: single `httpx.AsyncClient` instantiated at bot startup, reused
across calls, with `Keep-Alive`. `make dev` (or `docker-compose up`) launches
both processes with one command.

### 2.4 Database: **SQLite + SQLAlchemy ORM**

**Council verdict:** unanimous Option (a).

Reasoning: zero-config, file-based, ACID, reproducible across reviewer machines.
SQLAlchemy keeps schema readable, prevents injection, and the upgrade path to
Postgres is a one-line connection string swap. Raw `sqlite3` is more
error-prone SQL; Postgres is ops burden; DuckDB is OLAP-shaped.

DB file location: `./data/ehr.db` (gitignored). Schema initialised at startup
via `Base.metadata.create_all`. Seed script (`scripts/seed.py`) loads a few
demo providers and patients for manual testing.

### 2.5 Project layout: **`src/prosper/` package**

**Council verdict:** Option (b) `src/` layout over flat or domain-split.

Reasoning: production-grade Python convention, avoids import shadowing bugs,
clean separation from `tests/` and `evals/`, integrates with pytest and
pyproject.toml naturally.

```
prosperai/
├── bot.py                         # entrypoint; thin wrapper around src.prosper.bot
├── pyproject.toml
├── Makefile
├── docker-compose.yml
├── README.md
├── SOLUTION.md
├── env.example
├── data/                          # .gitignored — sqlite db
├── docs/superpowers/
│   ├── specs/                     # this file
│   └── plans/                     # impl plan
├── src/prosper/
│   ├── __init__.py
│   ├── bot.py                     # pipecat pipeline wiring
│   ├── dispatcher.py              # FSM dispatcher (state transitions + tool whitelist)
│   ├── flows.py                   # state definitions, transitions, system prompts per state
│   ├── prompts.py                 # per-state system-prompt templates (≤ 1 KB each)
│   ├── tools.py                   # OpenAI tool/function schemas + handlers calling EHR via httpx
│   ├── ehr/
│   │   ├── __init__.py
│   │   ├── api.py                 # FastAPI app exposing 5 endpoints
│   │   ├── models.py              # SQLAlchemy models
│   │   ├── repository.py          # DB queries (find, create, list, cancel)
│   │   └── schemas.py             # Pydantic request/response models
│   ├── observability/
│   │   ├── __init__.py
│   │   └── timing.py              # structured timing logs + p50/p95 aggregator
│   └── ehr_client.py              # httpx async client wrapper used by tools.py
├── tests/                         # pytest unit tests (EHR + dispatcher)
└── evals/
    ├── __init__.py
    ├── conftest.py
    ├── scenarios/                 # YAML files: scripted user turns + expected outcomes
    │   ├── new_patient_book.yaml
    │   ├── existing_patient_cancel.yaml
    │   ├── multiple_appointments.yaml
    │   ├── unavailable_slot.yaml
    │   ├── cancel_nonexistent.yaml
    │   └── ambiguous_dob.yaml
    ├── judge.py                   # LLM-as-judge runner
    ├── runner.py                  # feeds scripted turns through dispatcher, captures transcript + tool calls + final DB state
    ├── audio_smoke/               # ~3 full-pipeline tests (TTS→STT loop)
    │   └── test_audio_smoke.py
    └── test_scripted.py           # pytest entrypoint: runs all yaml scenarios
```

### 2.6 Patient identification strategy: **Phone first, fallback to name+DOB**

**Council verdict:** Option (b).

Reasoning: phone is a digit string — STT handles it crisply. Name and DOB have
many spoken forms ("ten five" vs "October fifth"). Phone-first cuts friction.
The challenge spec requirement `find_patient by name+DOB` is fully implemented
and exercised on the fallback path — eval suite asserts both paths fire across
scenarios.

Flow inside `IDENTIFY_PATIENT`:
1. Ask "What's the best phone number to find you under?"
2. Call `find_patient_by_phone(phone)`.
3. If 1 match → confirm name aloud, transition to `CHOOSE_INTENT`.
4. If 0 matches → ask name + DOB → call `find_patient_by_name_dob(name, dob)`.
5. If still 0 matches → transition to `REGISTER_PATIENT`.
6. If >1 matches at any step → ask DOB (or "did you mean John or Jonathan?") to
   disambiguate.

Implementation details:
- Name normalised (NFKD unicode, lowercased, whitespace collapsed, honorifics
  stripped) before fuzzy match. Repository returns top-N candidates with
  similarity score (RapidFuzz `token_sort_ratio`).
- DOB parsed with `dateutil.parser` (tolerant of "January twelfth nineteen
  eighty-six", "1/12/86", "12 jan 86", etc.). If ambiguous, dispatcher asks
  "Did you mean January 12, 1986?" before submitting.

### 2.7 Appointment cancellation: **Adaptive 0/1/N**

**Council verdict:** unanimous Option (c).

After identifying the patient, repository returns their upcoming appointments:
- 0 upcoming → bot says "I don't see any upcoming appointments under your
  name." → END.
- 1 upcoming → bot reads it ("You have one upcoming visit, Tuesday May 26 at
  3 PM with Dr. Patel. Cancel that one?") → on yes, CONFIRM → cancel → END.
- N upcoming → bot reads numbered list ("I see three upcoming visits: one,
  Tuesday May 26 at 3 PM with Dr. Patel; two, …; three, …. Which one would
  you like to cancel?"). Accept ordinal ("the first one") or date phrase
  ("the one on Tuesday"). If ambiguous, re-list.

### 2.8 Eval suite design: **Hybrid scripted text + ~3 audio smoke tests + paired judge + state assertion**

**Council verdict:** unanimous Option (c). v2 reinforcements (council 5):
declarative `Scenario` dataclass, **state assertion paired with LLM judge**,
optional `HeadlessFlow` runner, optional `--baseline` regression diff.

**Scripted text evals (bulk, CI-runnable):**

Scenarios are plain Python data, one entry per file in `evals/scenarios.py`.
This is the killer pattern from PauMinguet's submission: adding a scenario is
a small data PR, not a framework change.

```python
@dataclass
class Scenario:
    name: str
    tags: frozenset[str]                 # "happy", "recovery", "adversarial", "edge"
    persona: str                          # system prompt for the simulator LLM
                                          # — scripts the user side of the call,
                                          # including any deliberate mistake
    setup: Callable[[Session], None]      # seeds DB before run (patients, slots,
                                          # existing appointments)
    expected_state: StateExpectation      # post-conditions checked against DB
    judge_criteria: list[str]             # natural-language rubric for LLM judge
    max_turns: int = 12                   # cap to prevent infinite loops

@dataclass
class StateExpectation:
    patient_count_delta: int = 0
    active_appointment_count_delta: int = 0
    cancelled_appointment_count_delta: int = 0
    expected_terminal_state: Optional[str] = None  # FSM state name
    expected_tool_call_codes: list[str] = field(default_factory=list)
    forbidden_tool_calls: list[str] = field(default_factory=list)
```

Runner mechanics (`evals/runner.py`):
1. Spin up a fresh in-memory SQLite EHR (via `httpx.ASGITransport`, no separate
   uvicorn process during evals — hermetic and ~10× faster).
2. Call `scenario.setup(session)` to seed the DB.
3. Snapshot DB counts before the run.
4. Drive the conversation: a persona-LLM (with `scenario.persona` as its
   system prompt) generates user turns; the dispatcher consumes them and
   produces agent responses, tool calls, state transitions. Cap at
   `scenario.max_turns`.
5. After the run, perform **paired checks**:
   - **State assertion (deterministic):** query DB counts, compare to
     `expected_state.*_delta`. Assert `forbidden_tool_calls` never fired.
     Assert `expected_tool_call_codes` all fired. Assert `expected_terminal_state`
     reached. Any miss → FAIL with structured reason.
   - **LLM judge (semantic):** pass transcript + `judge_criteria` to a cheap
     LLM (gpt-5-mini), get PASS/FAIL + one-line justification. This catches
     things state checks can't: "did the bot read the appointment back to the
     user before cancelling it?"
6. Both must PASS for the scenario to pass — closes the "judge said yes but
   nothing happened" gap.

Scenarios at launch (6 mandatory, more added as bugs are caught):

| Name                       | Tags             | What it tests                                        |
|----------------------------|------------------|------------------------------------------------------|
| `new_patient_books`        | happy            | Phone-first lookup → no match → register → book      |
| `existing_patient_cancels` | happy            | Phone match → 1 upcoming → adaptive auto-confirm → cancel |
| `cancel_picks_from_list`   | happy            | 3 upcoming → numbered list → ordinal pick → cancel    |
| `dob_misheard_then_corrected` | recovery       | Persona spells DOB wrong on first try, corrects after read-back |
| `slot_taken_by_other`      | edge             | Persona requests a slot that gets taken mid-call (setup adds an appointment after slot read but before booking) — agent must offer alternatives |
| `cancel_when_nothing_to_cancel` | edge        | Patient has 0 upcoming — agent must say so, not invent one |

CLI: `pytest evals/test_scripted.py` (default). `python -m evals.runner --json results.json --baseline previous.json` (CI gate: exits non-zero on regression). Target <30 s wall clock for the whole suite. Logged metrics per scenario: pass/fail (state + judge), latency per turn, total tool calls, cached-token fraction (if available — `COULD` per council, log even if not surfaced in v1).

**HeadlessFlow runner (SHOULD, Day 3 if on track):** the runner above bypasses Pipecat entirely by importing the dispatcher and node definitions directly — same FSM, same prompts, same tools, just no audio pipeline. This decouples LLM-behaviour testing from STT/TTS/WebRTC and makes evals reproducible. Borrowed conceptually from PauMinguet `evals/runner.py`.

**Audio smoke tests (~3, manual or nightly):**
- Full pipeline: synthesised caller audio → bot STT → bot logic → bot TTS →
  caller STT → judge. Catches regressions where text-level tests pass but the
  STT mis-hears the LLM's response or TTS chokes on an unusual character.
- Scenarios: new-patient-book happy path, existing-patient-cancel happy path,
  one "noisy DOB" case (mumbled date).
- Runs via `pytest evals/audio_smoke -m audio`. Marked separately so default
  CI doesn't pull ElevenLabs credits on every push.

### 2.9 Latency tactics: **Instrument + long-stable persona for prompt cache + tight per-state prompts (+ stretch: availability cache)**

**v2 addition (MUST #6):** the system message is structured for OpenAI prompt
caching. A ≥1024-token `CLINIC_PERSONA` preamble lives at the top of every
LLM call — it doesn't change within a session or across sessions. Per-state
`task_messages` are short and specific, appended after the persona. OpenAI
caches the preamble after the first hit, dropping per-turn input tokens
~80% on cached calls (PauMinguet reports 75–85% cache hit rate in his eval
table — we will measure and report ours).

The persona content describes: clinic identity (Prosper Health), the agent's
job (booking and cancellation), the canonical interaction style (warm, brief,
read back what matters), how to confirm before mutating state, what timezone
to assume, and what to say if a tool call fails. It is **stable** — once
written, it is not edited mid-sprint.


**Council ranking:** (i) instrumentation > (v) tight prompts > (ii) cache >
(iv) streaming TTS > (iii) proactive prefetch.

**Implement:**
- **(i) Structured timing logs.** Every external call (STT chunk, LLM
  request, TTS request, EHR HTTP call) emits a JSON log line with
  `session_id`, `state`, `phase`, `duration_ms`. `observability/timing.py`
  aggregates and prints p50/p95 per phase at session end. SOLUTION.md includes
  a measured table.
- **(v) Tight per-state system prompts.** Each prompt in `prompts.py` capped
  at ≤ 1 KB. Per-turn LLM context excludes irrelevant history (only carries
  identified-patient fields and current state's slot data).

**Stretch (if time after eval suite):**
- **(ii) AvailabilityCache.** Plain `dict` keyed by `(date, provider_id)`
  with TTL=60s in front of `list_availability_slots`. Cache invalidates
  immediately on `create_appointment` for the matching key.

**Skipped (documented as future work in SOLUTION.md):**
- **(iv) Streaming TTS** — high perceived-latency win but unknown ROI before
  measurement, vendor credit cost.
- **(iii) Proactive prefetch on partial STT** — brittle (partials change),
  high FSM complexity; revisit once (i) data shows where pain is.

---

## 3. Data model

**v2 change:** availability is no longer computed from working-hours arithmetic.
Each bookable opening is a pre-seeded `Slot` row. An admin can block lunch or
PTO by inserting an exception row. Booking is a foreign key from the
appointment to the slot, which makes idempotency a unique constraint instead
of a distributed-lock problem.

```python
class Provider(Base):
    id: UUID (pk)
    name: str
    timezone: str  # IANA e.g. "America/New_York"

class Patient(Base):
    id: UUID (pk)
    first_name: str
    last_name: str
    name_normalized: str  # lowercased ASCII, whitespace-collapsed, no honorifics — used for fuzzy match
    dob: date
    phone: str (unique, indexed)  # E.164 normalised
    email: Optional[str]
    created_at: datetime

class Slot(Base):
    id: UUID (pk)
    provider_id: UUID (fk → Provider.id, indexed)
    start_at: datetime (UTC, indexed)
    end_at: datetime (UTC)
    is_blocked: bool  # True = admin-blocked (lunch, PTO); query excludes these
    created_at: datetime

    # Unique (provider_id, start_at) — no two slots at the same instant for the same provider.

class Appointment(Base):
    id: UUID (pk)
    patient_id: UUID (fk → Patient.id, indexed)
    slot_id: UUID (fk → Slot.id, indexed, unique-when-active)
    status: Enum("scheduled", "cancelled", "completed")
    created_at: datetime
    cancelled_at: Optional[datetime]
    notes: Optional[str]

    # Partial unique index on slot_id WHERE status='scheduled' — one active
    # appointment per slot. Cancelled rows accumulate freely for audit.
```

Seed script (`scripts/seed.py`) inserts three providers and ~120 slots across
the next 14 days (9am–5pm, 30-minute granularity, skipping lunch), plus a few
demo patients. Reviewers get a usable calendar out of the box.

---

## 4. HTTP API contracts

All endpoints return JSON. All write endpoints accept an `Idempotency-Key`
header (optional but recommended) so the LLM can safely retry without
double-writes.

| Method | Path                          | Body / Query                                     | Response                                |
|--------|-------------------------------|--------------------------------------------------|-----------------------------------------|
| POST   | `/patients`                   | `{first_name, last_name, dob, phone, email?}`    | `201 {patient}` or `409 {existing}`     |
| GET    | `/patients/by-phone`          | `?phone=`                                        | `200 {patients: [...]}` (possibly empty) |
| GET    | `/patients/by-name-dob`       | `?name=&dob=&min_similarity=0.85`                | `200 {patients: [..., similarity]}`     |
| GET    | `/patients/{id}/appointments` | `?status=scheduled&from=now`                     | `200 {appointments: [...]}`             |
| GET    | `/availability`               | `?date=YYYY-MM-DD&provider_id=`                  | `200 {slots: [{id, start_at, end_at, provider_id, provider_name}]}` — returns un-booked, un-blocked Slot rows |
| POST   | `/appointments`               | `{patient_id, slot_id, notes?}` (book by slot id, not start_at) | `201 {appointment}` or `409 {conflict, existing_appointment}` |
| POST   | `/appointments/{id}/cancel`   | `{reason?}`                                      | `200 {appointment}` or `404 {error}`    |

**v2 idempotency change:** `POST /appointments` first checks for an active
appointment on the requested `slot_id`. If one exists for the *same* patient,
return `200` with the existing row (LLM retry case). Different patient →
`409` with the conflicting appointment. No `Idempotency-Key` header needed —
slot_id functions as the natural idempotency key.

Errors are RFC 7807 `application/problem+json` shaped.

Mapping to challenge-required endpoints:
- `create_patient` → `POST /patients`
- `find_patient` → `GET /patients/by-phone` or `GET /patients/by-name-dob`
- `list_availability_slots` → `GET /availability`
- `create_appointment` → `POST /appointments`
- `cancel_appointment` → `POST /appointments/{id}/cancel`

---

## 5. LLM tool schemas (OpenAI function-calling)

Exposed to the LLM via per-state whitelists. Each tool's handler in `tools.py`
calls the EHR via the shared `httpx.AsyncClient` and returns a small structured
result for the LLM to read.

Names and JSON-schema parameter shapes (truncated for readability):

- `find_patient_by_phone(phone: string)` → `{found: bool, patients: [{id, first_name, last_name, dob}]}`
- `find_patient_by_name_dob(name: string, dob: string)` → `{found: bool, patients: [{id, first_name, last_name, dob, similarity}]}`
- `create_patient(first_name, last_name, dob, phone, email?)` → `{patient_id, ...}`
- `list_availability_slots(date: string, provider_id?: string)` → `{slots: [{slot_id, start_at_iso, end_at_iso, provider_id, provider_name}]}`
- `create_appointment(patient_id, slot_id, notes?)` → `{appointment_id, start_at, provider_name}` or `{conflict: true, owner: "same_patient"|"other_patient"}`
- `get_upcoming_appointments(patient_id)` → `{appointments: [{id, start_at, provider_name}]}`
- `cancel_appointment(appointment_id, reason?)` → `{ok: true, appointment_id}` or `{error}`

All time values cross the wire as ISO-8601 in clinic timezone for LLM
readability; the EHR converts to UTC internally.

**v2 typed returns (SHOULD #10):** every tool handler in `src/prosper/tools.py`
returns a `Result[Ok, Err]` discriminated union. Both variants are dataclasses
with a `kind` literal field. Eliminates `None | dict | list` ambiguity at the
boundary, lets the dispatcher pattern-match cleanly, and makes mypy actually
useful here. Sketch:

```python
@dataclass
class Ok(Generic[T]):
    kind: Literal["ok"] = "ok"
    value: T

@dataclass
class Err:
    kind: Literal["err"] = "err"
    code: str          # e.g. "patient_not_found", "slot_taken_other_patient"
    message: str       # for logs only, never voiced verbatim
    retryable: bool

Result = Ok[T] | Err
```

The `code` enum is also what scenario judges assert against (e.g.
`expected_state.last_tool_result_code == "slot_taken_other_patient"`).

---

## 6. Failure handling

- **EHR call fails / 5xx:** dispatcher catches, says "I'm having trouble
  reaching the clinic's system right now, give me a moment", retries once with
  300 ms backoff. On second failure → "Let me transfer you to a person" and
  ends gracefully.
- **LLM hallucinates a non-whitelisted tool:** dispatcher rejects, injects a
  system message ("That tool isn't available in this step"), LLM retries.
- **Double-book attempt:** EHR returns 409, tool result tells LLM "that slot
  just got taken, here are alternatives" with fresh availability.
- **Cancel non-existent appointment:** EHR 404, tool result tells LLM, LLM
  apologises and either lists actual appointments or ends.
- **STT misheard digit in phone:** if `find_patient_by_phone` returns 0
  matches, fall through to name+DOB path (handles this case naturally).
- **Confirmation requirement:** all writes (create patient, book, cancel) are
  gated behind explicit user "yes" in `CONFIRM` state. The dispatcher reads
  back the salient details first.

---

## 7. Testing strategy

Layered:

| Layer                    | Mechanism                             | Run frequency |
|--------------------------|---------------------------------------|---------------|
| Unit (EHR endpoints)     | pytest + FastAPI `TestClient` + in-memory SQLite | every commit |
| Unit (dispatcher)        | pytest + mocked LLM responses + mocked EHR | every commit |
| Scripted scenario evals  | `pytest evals/test_scripted.py` (real LLM, in-memory EHR, LLM-as-judge) | every commit |
| Audio smoke              | `pytest evals/audio_smoke -m audio` (full pipeline) | nightly / pre-deploy |

Unit tests target ≥80% coverage on `src/prosper/ehr/` and `src/prosper/`.
Scenario evals target 6 base scenarios listed in §2.5 plus regression cases
added when bugs are found.

---

## 8. Reviewer experience (target)

- Reviewer clones repo, `cp env.example .env`, fills two API keys.
- Runs `make dev` (or `docker-compose up`) — two processes start, browser opens
  to `http://localhost:7860`.
- Talks to the bot, books a real appointment, cancels it. Data persists across
  bot restarts (SQLite file).
- Runs `make eval` — scripted scenarios pass, latency table prints to stdout.
- Reads `SOLUTION.md` — see why decisions were made, what was deliberately cut,
  what's marked as future work.
- Reads `docs/superpowers/specs/2026-05-19-prosper-challenge-design.md` if
  curious about deliberation depth.

---

## 8.5 Code-quality scaffolding (v2 additions)

**`CLAUDE.md` at repo root (MUST #12):** committed file with hard rules for any
future LLM working in this repo. Sample rules:
- Fix root causes, never silence errors.
- Every public function in `src/prosper/` has a type annotation and a one-line
  docstring.
- Adding a feature requires adding (or extending) at least one eval scenario.
- Tool handlers return `Result[Ok, Err]` — never bare `dict | None`.
- Per-state system prompts stay ≤ 1 KB.
- Voice copy lives in `prompts.py` constants, never inline in dispatcher code.

**Pre-commit (MUST #13):** `.pre-commit-config.yaml` with:
- `ruff format` + `ruff check --fix`
- `mypy --strict src/prosper`
- `pytest tests/` (unit only, fast)

**GitHub Actions CI (MUST #13):** single workflow `.github/workflows/ci.yml`:
- Lint (ruff)
- Type-check (mypy)
- Unit tests (`pytest tests/`)
- Scripted evals (`pytest evals/test_scripted.py`) — requires `OPENAI_API_KEY`
  as a GH secret; skipped if absent so external forks still get lint/type/unit.
- Audio smoke tests excluded from CI (`-m 'not audio'`).

## 9. Intentional cuts (documented in SOLUTION.md)

| Cut                                | Reason                                                      |
|------------------------------------|-------------------------------------------------------------|
| No auth / OAuth                    | Single-clinic demo; security layer outsourced to provider in prod |
| No HIPAA encryption-at-rest        | Demo scope; SQLite file is dev-only — document upgrade path |
| No multi-provider TTS/LLM fallback | Two-line adapter swap if added; left as documented future work |
| No CI per-PR Docker build          | Out of scope; `make eval` covers correctness gate           |
| No OpenTelemetry / Prometheus       | Structured stdout JSON logs sufficient for challenge scope  |
| SQLite instead of Postgres         | SQLAlchemy makes the swap trivial; file-backed is reproducible |
| No streaming TTS                   | Skipped pending measurement data; flagged as future work    |
| No proactive prefetch              | Brittle on STT partials; revisit after seeing real latency data |

---

## 10. Open questions / risks

- **Pipecat hooks for partial STT:** verified to exist (`STTService` emits
  `TranscriptionFrame` partials). If proactive prefetch is added later, the
  hook is in place.
- **OpenAI tool-call latency vs prompt size:** measurement (i) will quantify
  whether keeping prompts <500 tokens is sufficient or if we need to split
  further.
- **DOB parsing edge cases:** `dateutil.parser` covers most spoken forms but
  may misinterpret "twenty twenty-six" as 2026 vs 20/20/26 — add explicit
  confirmation step regardless.

---

## 10.5 SOLUTION.md structure (v2 — MUST/SHOULD additions)

Sections, in order, each with a strict word budget so the doc stays readable:

1. **Overview** — 2 sentences, what the project does and the runtime topology.
2. **How to run** — `cp env.example .env`, fill keys, `make dev`, click connect.
3. **How to evaluate** — `make eval` runs the scenario suite, output explained.
4. **Architecture decisions** — bullet list pointing to this spec; brief
   rationale per choice with the FSM/EHR-process/DB picks called out.
5. **Conversation flow** — state diagram (ASCII) + per-state tool whitelist
   table.
6. **Eval suite** — what state assertion + LLM judge each cover, why both.
7. **Latency** — measured `p50/p95` table per phase from a real run. Brief
   commentary on what surprised us.
8. **Real transcript (MUST #15):** one full successful new-patient-book run
   end-to-end. One failure run (e.g. ambiguous DOB recovery). Both lifted
   verbatim from a session log — no editing.
9. **Dev-log (SHOULD #16):** half-page bullets — what we tried that didn't
   work, what surprised us, what we'd do differently.
10. **Intentional cuts** — table from §9.
11. **Future work** — items deferred from this build (OpenRouter LLM fallback,
    streaming TTS, proactive prefetch, prompt-injection eval, cached-token
    counter, etc.).

## 11. Build sequence (preview — full plan in writing-plans phase)

Updated for v2:

1. EHR FastAPI service + SQLAlchemy models (Provider/Patient/**Slot**/Appointment) + seed script + unit tests
2. `httpx` EHR client + `Result[Ok, Err]` tool handlers + dispatcher skeleton (no Pipecat yet)
3. Long stable `CLINIC_PERSONA` preamble + per-state prompt templates + tool whitelists + FSM transition rules
4. `Scenario` dataclass + `StateExpectation` + first scripted runner (no Pipecat — pure dispatcher tests). Seed 6 base scenarios. LLM judge wired up. Paired state+judge assertion.
5. Pipecat pipeline wiring + dispatcher integration + manual smoke through browser
6. Latency instrumentation + p50/p95 aggregation + persona-cache hit reporting
7. `CLAUDE.md` + pre-commit (ruff+mypy) + GH Actions CI
8. (SHOULD) `--baseline previous.json` regression diff
9. (Stretch) availability cache
10. Audio smoke tests (3 scenarios, marker-gated)
11. SOLUTION.md with measured latency table + real transcript + dev-log
12. README polish + final manual end-to-end run through browser

---

*Council deliberation logs (4 sessions, 12 model responses + 4 chairman
syntheses) archived in conversation history. Every architectural decision
above maps to a council verdict line — no choices were made by fiat.*
