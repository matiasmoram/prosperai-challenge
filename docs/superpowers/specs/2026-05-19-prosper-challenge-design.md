# Prosper Health Challenge — Design Spec

**Author:** Matías (with LLM council deliberations)
**Date:** 2026-05-19
**Ambition:** Plan B — solid core + two well-executed bonuses
**Bonuses:** Automated eval suite (LLM-as-judge) + latency handling

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

### 2.8 Eval suite design: **Hybrid scripted text + ~3 audio smoke tests**

**Council verdict:** unanimous Option (c).

**Scripted text evals (bulk, CI-runnable):**
- Each scenario is a YAML file describing: initial DB state (patients,
  appointments, providers); ordered list of user turns (plain text); expected
  final DB mutations; expected terminal state.
- `evals/runner.py` instantiates dispatcher with a fresh in-memory SQLite EHR,
  pumps user turns through `dispatcher.handle_user_turn(text)`, captures
  every transition + every tool call + the final transcript and DB state.
- `evals/judge.py` calls a cheap LLM (gpt-5-mini) with a deterministic rubric:
  did the agent reach the expected terminal state? did it perform the expected
  EHR mutations? did it ask for required confirmations? PASS/FAIL + one-line
  justification.
- Assertions also check tool-call sequence directly (not just final state) so a
  test fails fast if the agent calls `cancel_appointment` without first
  identifying the patient.
- Runs via `pytest evals/test_scripted.py`. Target <30 s total wall clock.
- Logged metrics per scenario: pass/fail, latency per turn, total tool calls.

**Audio smoke tests (~3, manual or nightly):**
- Full pipeline: synthesised caller audio → bot STT → bot logic → bot TTS →
  caller STT → judge. Catches regressions where text-level tests pass but the
  STT mis-hears the LLM's response or TTS chokes on an unusual character.
- Scenarios: new-patient-book happy path, existing-patient-cancel happy path,
  one "noisy DOB" case (mumbled date).
- Runs via `pytest evals/audio_smoke -m audio`. Marked separately so default
  CI doesn't pull ElevenLabs credits on every push.

### 2.9 Latency tactics: **Instrument + tight prompts (+ stretch: availability cache)**

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

class Appointment(Base):
    id: UUID (pk)
    patient_id: UUID (fk → Patient.id, indexed)
    provider_id: UUID (fk → Provider.id)
    start_at: datetime (UTC, indexed)
    end_at: datetime (UTC)
    status: Enum("scheduled", "cancelled", "completed")
    created_at: datetime
    cancelled_at: Optional[datetime]
    notes: Optional[str]

    # Unique constraint (provider_id, start_at) where status='scheduled' — DB-level
    # protection against double-booking even if LLM hallucinates slot args.
```

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
| GET    | `/availability`               | `?date=YYYY-MM-DD&provider_id=&duration_minutes=30` | `200 {slots: [{start_at, end_at, provider_id}]}` |
| POST   | `/appointments`               | `{patient_id, provider_id, start_at, end_at, notes?}` | `201 {appointment}` or `409 {conflict}` |
| POST   | `/appointments/{id}/cancel`   | `{reason?}`                                      | `200 {appointment}` or `404 {error}`    |

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
- `list_availability_slots(date: string, provider_id?: string, duration_minutes?: int)` → `{slots: [{start_at_iso, end_at_iso, provider_id, provider_name}]}`
- `create_appointment(patient_id, provider_id, start_at, end_at, notes?)` → `{appointment_id, start_at, provider_name}` or `{conflict: true}`
- `get_upcoming_appointments(patient_id)` → `{appointments: [{id, start_at, provider_name}]}`
- `cancel_appointment(appointment_id, reason?)` → `{ok: true, appointment_id}` or `{error}`

All time values cross the wire as ISO-8601 in clinic timezone for LLM
readability; the EHR converts to UTC internally.

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

## 11. Build sequence (preview — full plan in writing-plans phase)

1. EHR FastAPI service + SQLAlchemy models + unit tests
2. `httpx` EHR client + tool handlers + dispatcher skeleton (no Pipecat yet)
3. Pipecat pipeline wiring + dispatcher integration
4. Per-state prompts + tool whitelists + FSM transition rules
5. Scripted eval runner + 6 base scenarios + LLM-as-judge
6. Latency instrumentation + p50/p95 aggregation
7. (Stretch) availability cache
8. Audio smoke tests
9. SOLUTION.md + README polish
10. Manual end-to-end run through browser

---

*Council deliberation logs (4 sessions, 12 model responses + 4 chairman
syntheses) archived in conversation history. Every architectural decision
above maps to a council verdict line — no choices were made by fiat.*
