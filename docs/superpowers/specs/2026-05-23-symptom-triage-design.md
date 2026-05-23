# Symptom-based triage + variable visit duration

Date: 2026-05-23
Status: approved (brainstorming sections 1-4)
Author: Claude + Matías

## Problem

Today the agent's `BOOK_FLOW` assumes the caller already knows the specialty
they want and books a fixed 30-min slot. Real callers often describe symptoms
("hey, me pasa esto, qué hago?") instead of naming a provider. Same caller can
need a 30-min GP check vs a 60-min therapy session — provider time should
match.

## Solution (Approach B, approved)

Add a `suggest_specialty(symptoms)` tool exposed inside the existing
`BOOK_FLOW` state. The handler runs a single mini-LLM JSON-mode call
(`gpt-4o-mini`, no tools) that classifies the symptom description into one
of the seeded specialties and assigns a duration. Main LLM then calls
`list_availability_slots(specialty, duration_minutes)` filtered to slots
that can host the visit (consecutive free slots when duration > 30).

FSM topology unchanged. No new state.

## Components

| File | Change |
|---|---|
| `src/prosper/prompts.py` | `SPECIALTY_DURATION_TABLE` (default minutes per seeded specialty), `TRIAGE_SYSTEM_PROMPT` (mini-LLM system), persona footnote: "if caller describes symptoms, call `suggest_specialty`; if caller names a specialty/doctor, skip it." |
| `src/prosper/llm.py` | `classify_symptoms(symptoms) -> Result[SpecialtyClassification, Err]`. JSON-mode `gpt-4o-mini`, no tools, temperature=0, no retries beyond OpenAI client's. |
| `src/prosper/tools.py` | `suggest_specialty_handler` (returns `Result`). Extend `list_availability_slots_handler` with optional `duration_minutes`. Extend `create_appointment_handler` with `duration_minutes`. New TOOL_SCHEMAS entry. |
| `src/prosper/flows.py` | `ALLOWED_TOOLS[BOOK_FLOW]` += `"suggest_specialty"`. |
| `src/prosper/dispatcher.py` | `SessionMemory` += `recommended_specialty`, `recommended_duration_minutes`. `_redact_for_llm` clause for `suggest_specialty`. `_record_tool_result` stores classification. |
| `src/prosper/ehr/models.py` | `Appointment.duration_minutes` (int, default 30). New `AppointmentSlotLock` table (PK on `slot_id`) — DB-level multi-slot lock. Keep `uq_appointment_active_slot` for backward compat. |
| `src/prosper/ehr/repository.py` | `list_available_slots` adds `duration_minutes` param → filters to slots where N consecutive are free same provider. `create_appointment` adds `duration_minutes` + creates N junction rows in one txn. `cancel_appointment` deletes locks. `reschedule_appointment` deletes old locks + creates new locks atomically. New `NoConsecutiveSlotsError`. |
| `src/prosper/ehr/api.py` | `/availability` query param `duration_minutes` (default 30). `/appointments` body field `duration_minutes` (default 30). `/appointments/{id}` PATCH may include new duration. |
| `src/prosper/ehr/schemas.py` | `AppointmentCreate.duration_minutes: int = 30`. `AppointmentReschedule.duration_minutes: int \| None = None`. `AppointmentOut.duration_minutes`. |
| `src/prosper/ehr_client.py` | Thread `duration_minutes` through `list_availability`, `create_appointment`, `reschedule_appointment`. |
| `tests/test_triage.py` | Unit: handler returns Ok with mocked mini-LLM. Low-confidence path. Invalid-specialty rejection. Red-flag escalation. |
| `tests/ehr/test_repository.py` | Consecutive-slot search 30/60/90. Multi-slot booking atomicity (race). Cancel releases all locks. Reschedule swaps locks. |
| `evals/mock_llm.py` | Deterministic mock for `suggest_specialty` keyed on persona keywords. |
| `evals/scenarios.py` | `symptom_routes_to_specialty`, `ambiguous_followup_then_book`, `direct_specialty_skips_triage`. |
| `docs/adr/005-symptom-triage.md` | ADR justifying mini-LLM router + junction-table multi-slot lock. |
| `docs/glossary.md` | Add "triage", "junction lock", new err codes. |

## Data flow (happy path)

```
Caller: "Me he encontrado muy mal del estómago"
↓
Main LLM (BOOK_FLOW): tool_call suggest_specialty(symptoms="stomach unwell")
↓
Handler → classify_symptoms (mini-LLM gpt-4o-mini JSON-mode)
       → Ok{specialty="General Practice", duration_minutes=30, confidence=0.85, follow_up=None}
↓
Main LLM: "Te recomiendo médico de cabecera. ¿Qué día?"
Caller: "Mañana mañana"
↓
Main LLM: tool_call list_availability_slots(date, specialty="General Practice", duration_minutes=30)
↓
Repo: returns slots where this one and next-(N-1) consecutive are free same provider
↓
Caller picks → CONFIRM_BOOK → create_appointment(slot_id, duration_minutes=30) → 1 junction row
```

For duration=60: same path, but `list_availability_slots` returns only slots where the next 30-min slot is also free under same provider. `create_appointment` inserts 2 `AppointmentSlotLock` rows in one transaction.

## Error contract additions

| Code | When | Retryable |
|---|---|---|
| `triage_unavailable` | mini-LLM timeout / 5xx / JSON malformed | yes |
| `unknown_specialty` | mini-LLM returns specialty not in `SPECIALTY_DURATION_TABLE` | falls back to GP |
| `low_triage_confidence` | confidence < 0.5 | one follow-up via `follow_up` field |
| `medical_emergency` | red-flag keywords detected by mini-LLM | escalate (never retry) |
| `invalid_duration` | duration not in `{30, 60, 90}` | yes |
| `no_consecutive_slots` | duration > 30 but no contiguous block free | suggest other day or shorter duration |

## Tradeoffs

- **+1 OpenAI call** per booking turn (mini-LLM ~$0.15/1M, ~300 tokens prompt + ~100 response) → ~$0.0002 per triage. Acceptable.
- **Schema change**: junction table is the DB-level multi-slot lock. The previous `uq_appointment_active_slot` partial unique index stays but is now a subset guarantee — junction PK on `slot_id` is the load-bearing one.
- **Latency**: triage adds one round-trip (~500ms p95). Mitigated by `STATE_FILLERS` already firing on slow tool calls.
- **No multilingual**: explicitly out of scope per user.
- **No appointment-type selection**: user explicitly skipped — duration is per-specialty + LLM override only.

## Out of scope

- New specialties / providers (5 seeded suffice).
- Reschedule duration changes (allowed but not exercised by new scenarios).
- Healthie / external EHR.
- Vendor-failure handling (separate council deliberation, recommendation captured separately).

## References

- `OTHER_SOLUTIONS_REPORT.md` — peer analysis that motivated the feature.
- Council output (vendor failure decision) — separate.
- This repo's CLAUDE.md hard rules: `Result[Ok, Err]`, dispatcher gating, no `try/except: pass`, ≤1KB per `TASK_MESSAGES` entry, mypy --strict.
