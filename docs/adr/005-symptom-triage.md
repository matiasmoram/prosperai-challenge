# ADR 005 · Symptom triage + variable visit duration

**Status:** Accepted · 2026-05-23
**Context window:** Prosper Health voice-agent challenge, post-MVP feature wave.
**Supersedes / depends on:** ADR 001 (FSM dispatcher), ADR 002 (separate EHR), ADR 003 (paired state+judge).

## Context

Two gaps surfaced when comparing this agent to peer submissions
(`OTHER_SOLUTIONS_REPORT.md`):

1. **Callers don't always know which provider they need.** A real caller
   says "my stomach's been killing me, what do I do?" — not "book me a
   general-practice slot." The MVP `BOOK_FLOW` assumed the caller named a
   specialty.
2. **All visits were a fixed 30 minutes.** A first therapy intake and a
   repeat-prescription GP check are not the same length. Provider time
   should match the visit.

We want symptom-aware routing AND variable duration without bolting on a
second conversational framework or an external triage service, and
without breaking the existing invariants: typed `Result[Ok, Err]`,
dispatcher-only tool access, DB-enforced no-double-booking,
`mypy --strict`, hermetic mock-eval.

## Decision

### 1. A `suggest_specialty` tool backed by a mini-LLM

A new tool, available **only in `BOOK_FLOW`**, takes a free-form symptom
string and returns `{specialty, duration_minutes, confidence,
follow_up?}`. Its handler calls `llm.classify_symptoms` — a single
`gpt-4o-mini` JSON-mode call (strict schema, no tools, temperature 0, no
tenacity retry). The main LLM calls it only when the caller described
symptoms; if the caller named a specialty, the persona instructs it to
skip straight to `list_availability_slots`.

Rationale: the mini-LLM is cheap (~$0.0002/call), the JSON schema's
`enum` pins specialty + duration to legal values, and routing it through
a **tool** (not dispatcher pre-processing) keeps the FSM whitelist as the
single audit point. Low confidence (<0.7) returns a `follow_up` the main
LLM asks verbatim before re-classifying once; persistent ambiguity falls
back to General Practice. A `red_flag` (chest pain, suicidal ideation,
etc.) returns `Err(code="medical_emergency")` so the agent redirects to
emergency services and never books.

### 2. Variable duration via a junction-table slot lock

`Appointment` gains `duration_minutes ∈ {30, 60, 90}`. The 30-minute
slot grid is unchanged; a 60-minute visit occupies two consecutive
slots, 90 minutes three. The load-bearing lock moves from the partial
unique index on `Appointment.slot_id` to a new table:

```
AppointmentSlotLock(slot_id PK, appointment_id FK)
```

One row per occupied slot. PK on `slot_id` gives the same DB-level
double-booking guarantee as the old index but extends it to multi-slot
bookings: a concurrent writer claiming any slot in the chain hits the PK
constraint → `IntegrityError` → translated to `SlotTakenError` → 409.
`create_appointment` resolves the consecutive chain (same provider,
adjacent `start_at`, all unlocked) **before** insert and locks all N
rows in one transaction. `cancel_appointment` deletes the lock rows;
`reschedule_appointment` releases the old chain and acquires the new one
atomically (rolls back to the original if the new chain is unavailable).
`list_available_slots` gains `duration_minutes`: for >30 it returns only
anchor slots whose next (N-1) consecutive same-provider slots are free,
so any returned slot is always safe to book.

The partial unique index on `Appointment.slot_id` is kept as a
backward-compat subset guarantee for the single-slot common case.

## Consequences

**Positive**

- Natural booking: callers describe symptoms, the agent routes them.
- Provider time matches visit type.
- No new framework, no external service, no new infra.
- Every new failure mode is a typed `Err.code` (eval contract):
  `triage_unavailable`, `unknown_specialty`, `invalid_duration`,
  `low_triage_confidence` (surfaced via `follow_up`), `medical_emergency`,
  `no_consecutive_slots`.
- Mock-eval stays hermetic: the triage mini-LLM is stubbed by
  `MockTriageClient` (keyword routing) installed via
  `prosper.llm._TRIAGE_CLIENT_OVERRIDE`. Three new scenarios
  (`symptom_routes_to_gp`, `symptom_ambiguous_followup`,
  `direct_specialty_skips_triage`) run with paired state+judge.

**Negative / costs**

- +1 OpenAI round-trip per symptom-described booking (~500 ms p95). The
  existing `STATE_FILLERS` masks it.
- The triage mini-LLM is a second model dependency in the booking path.
  It fails closed (`triage_unavailable` → the agent asks the caller to
  name a specialty), so an outage degrades to MVP behaviour.
- A multi-slot booking can fragment the calendar (a lone 30-min gap
  between two 60-min visits). Acceptable at clinic scale.

**Explicitly out of scope**

- Multilingual routing.
- Appointment-type selection UI.
- Per-symptom provider sub-specialisation beyond the 5 seeded specialties.

## Verification

`make verify` (ruff + `mypy --strict` + pytest, incl. `tests/test_triage.py`
and the multi-slot cases in `tests/ehr/test_repository.py`) and
`make mock-eval` (49 scenarios, all paired state+judge) both pass.
