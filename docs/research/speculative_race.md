# Speculative Race Architecture — First-Turn Identity + Availability Prefetch

**Status:** Design proposal, pre-implementation
**Scope:** First turn of `IDENTIFY_PATIENT` state only. No schema changes, no prompt changes, no code written yet.

---

## 1. Problem Statement and Inspiration

A known voice-agent latency pattern: substantial latency can be cut by racing backend operations against the time a caller spends speaking. The key insight: when the user says their name, three things are simultaneously useful but only one will actually be needed:

1. Does this person already exist? (`find_patient_by_name_dob` / `find_patient_by_phone`)
2. If they don't exist, we'll need to create them — can we pre-validate the payload?
3. Regardless of identity outcome, the caller will want slots — can we prefetch today + next 3 business days?

The naive implementation fires these via `asyncio.create_task` backed by module-level globals — which leaks state badly: globals like `_current_patient_id` / `_creation_task` are never cleared on disconnect, so a re-used call silently inherits the previous session's state.

Our architecture is more disciplined: `SessionMemory` is scoped per `Dispatcher` instance, the dispatcher is the only tool path, and `identified_patient` is a hard gate. The speculative race must work within these constraints.

---

## 2. Constraint Summary (from codebase audit)

| Constraint | Source | Implication |
|---|---|---|
| Dispatcher is sole tool path | `flows.py` ALLOWED_TOOLS, `dispatcher.py` `_execute_tool` | Speculative tasks cannot call `HANDLERS[...]` directly |
| `identified_patient` gate is hard | `dispatcher.py` `_record_tool_result` lines 778-795 | Booking tools must not be mounted until gate passes |
| `create_patient` returns 409 if phone exists | `ehr/api.py` lines 137-151 | Speculative create-patient payload must NOT hit HTTP |
| Patient.phone is `unique=True` | `ehr/models.py` line 74 | DB-level safety net, but 409 reaches the caller |
| Slot uniqueness is partial index on `status=scheduled` | `ehr/models.py` lines 113-120 | Concurrent `create_appointment` races are safe (409 returned) |
| `find_patient_by_name_dob` uses `token_sort_ratio >= 0.85` | `ehr_client.py` → `ehr/api.py` → `repository.py` | Returns list; length 1 = unique match; length >1 = disambiguation needed |
| `EHRClient` is per-session | `dispatcher.py` `__init__`, `ehr_client.py` | No cross-session state leak risk |
| `asyncio.CancelledError` propagates through `await` | Python asyncio docs | Any `httpx.AsyncClient._request` in flight raises on cancel |
| `httpx` does not auto-retry on cancel | `ehr_client.py` `_request` | A cancelled in-flight HTTP POST leaves the request outcome unknown |

---

## 3. Sequence Diagrams (ASCII)

### Case A: Exact Match (one patient found, similarity >= 0.85, count == 1)

```
Caller says name
      │
      ▼
[IDENTIFY_PATIENT state entered]
      │
      ├──── asyncio.create_task(T_find)     ─── find_patient_by_name_dob(name, dob=None)
      │                                          [GET /patients/by-name-dob, no DOB filter yet]
      │                                          [name-only pass queries with min_similarity=0.85]
      │
      ├──── asyncio.create_task(T_avail)    ─── list_availability_slots for today + next 3 biz days
      │                                          [4 parallel GET /availability calls]
      │
      │     (NO T_create — payload incomplete until we know it's a new patient)
      │
      ▼
[asyncio.wait({T_find, T_avail}, return_when=FIRST_COMPLETED)]
      │
      │   T_find completes first: Ok(patients=[{id, first_name, last_name, dob, phone}])
      │
      ▼
[Race settler wakes]
 ├── T_find result: exactly 1 patient → store in SpeculationStore.find_result
 ├── Cancel T_avail? NO — keep it running (it will populate last_slots when done)
 ├── Set SpeculationStore.identity_resolved = True
 ├── Set SpeculationStore.identity_outcome = "found_exact"
 │
 ▼
[Dispatcher._record_tool_result("find_patient_by_name_dob", Ok(...))]
 ├── memory.identified_patient = patients[0]          ← gate opens
 ├── _transition("patient_found") → CHOOSE_INTENT
 │
 ▼
[T_avail completes (may still be running)]
 ├── Store result in SpeculationStore.avail_result
 ├── Populate memory.last_slots (pre-warm)
 ├── No FSM transition (slots are only consumed in BOOK_FLOW)
 │
 ▼
[Bot speaks: "Great, found you. Would you like to book or cancel?"]
[memory.last_slots already populated → BOOK_FLOW skips one LLM round-trip]
```

### Case B: Fuzzy Match, Single Close Result (similarity 0.85-0.94, count == 1)

```
Caller says name "Jon Smith" (DB has "John Smith", similarity 0.91)
      │
      ▼
[T_find completes: Ok(patients=[{..., similarity: 0.91}])]
      │
      ▼
[Race settler: 1 patient, high similarity but NOT exact name match]
 ├── SpeculationStore.identity_outcome = "found_fuzzy_single"
 ├── SpeculationStore.fuzzy_candidates = [patient]
 ├── SpeculationStore.identity_resolved = False  ← NOT resolved yet
 │
 ▼
[Dispatcher does NOT call _record_tool_result with identified_patient]
[Instead: inject disambiguation into LLM history]
 ├── Synthetic tool result: "Did you mean John Smith (DOB 1990-12-10)?"
 │
 ▼
[LLM asks caller: "Just to confirm — is that John Smith, born December 10th, 1990?"]
      │
      ▼
[Caller: "Yes"]
      │
      ▼
[Dispatcher: _affirm regex matches]
 ├── Resolve SpeculationStore.fuzzy_candidates[0] as identified_patient
 ├── memory.identified_patient = fuzzy_candidates[0]
 ├── _transition("patient_found")
 │
 ▼
[T_avail result (probably done by now) → memory.last_slots populated]
```

### Case C: Fuzzy Match, Multiple Close Results (count >= 2)

```
Caller says "Alex Johnson" (DB has "Alexandra Johnson" similarity 0.88
                            AND "Alexis Johnston" similarity 0.86)
      │
      ▼
[T_find completes: Ok(patients=[patient_A, patient_B])]
      │
      ▼
[Race settler: 2 candidates, both above threshold]
 ├── SpeculationStore.identity_outcome = "found_fuzzy_multiple"
 ├── SpeculationStore.fuzzy_candidates = [patient_A, patient_B]
 ├── SpeculationStore.identity_resolved = False
 │
 ▼
[Dispatcher injects disambiguation list into LLM history]
 ├── Synthetic result: "multiple matches: (1) Alexandra Johnson DOB 1985-03-04,
 │                      (2) Alexis Johnston DOB 1992-07-19 — which one?"
 │
 ▼
[LLM asks: "I found two patients that might be you — Alexandra Johnson born in 1985,
            or Alexis Johnston born in 1992. Which one is you?"]
      │
      ▼
[Caller identifies themselves (DOB or first name confirmation)]
      │
      ▼
[Dispatcher picks from fuzzy_candidates list]
 ├── memory.identified_patient = chosen_candidate
 ├── _transition("patient_found")
 │
 ▼
[T_avail result → memory.last_slots populated if caller hasn't reached BOOK_FLOW yet]
[If T_avail already timed out or was cancelled: BOOK_FLOW calls list_availability normally]
```

### Case D: No Match (count == 0 or all below threshold)

```
Caller says name — name not in DB
      │
      ▼
[T_find completes: Ok(patients=[])]
 ├── SpeculationStore.identity_outcome = "no_match"
 ├── SpeculationStore.identity_resolved = False
 │
 ▼
[Dispatcher: _transition("no_match") → REGISTER_PATIENT]
      │
      ├── T_avail: cancel it (new patient won't need slots for several turns)
      │           [T_avail.cancel() → CancelledError propagates through httpx calls]
      │           [await T_avail (with timeout) to drain the cancellation]
      │
      │   NOTE: T_create payload was NOT pre-built because we didn't have
      │   first_name/last_name/dob/phone yet. The LLM collects these now.
      │   (See Section 6: Why T_create is payload-prep only, not a real pre-task)
      │
      ▼
[Bot collects: first_name, last_name, dob, phone]
 ├── [All validated via _parse_dob, _phone_words_to_digits at tool boundary]
 │
 ▼
[LLM calls create_patient — normal FSM path, no pre-computation]
```

---

## 4. Data Model: SpeculationStore

The existing `SessionMemory` dataclass (`dispatcher.py` lines 78-89) should be extended with a new nested dataclass rather than polluting `SessionMemory` with speculation-specific fields. This keeps the public `SessionMemory` interface stable (eval scenarios assert on it) while isolating volatile in-flight state.

```python
@dataclass
class SpeculationStore:
    """Tracks in-flight speculative tasks for the first-turn race."""
    find_task: asyncio.Task[Result[dict[str, Any]]] | None = None
    avail_tasks: list[asyncio.Task[Result[dict[str, Any]]]] = field(default_factory=list)
    identity_resolved: bool = False
    identity_outcome: str | None = None  # found_exact | found_fuzzy_single |
                                          # found_fuzzy_multiple | no_match
    fuzzy_candidates: list[dict[str, Any]] = field(default_factory=list)
    find_result: Result[dict[str, Any]] | None = None
    avail_results: dict[str, Result[dict[str, Any]]] = field(default_factory=dict)
    patient_payload: SpeculativePatientPayload | None = None
    cleaned_up: bool = False
```

`SessionMemory` gains: `speculation: SpeculationStore | None = None`.

### Lifecycle

| Event | Action |
|---|---|
| `_transition("go_identify")` | Instantiate `SpeculationStore`, launch `T_find` + `T_avail_*` |
| `T_find` completes | Populate `find_result`, `identity_outcome`, `fuzzy_candidates` |
| `T_avail_*` completes | Populate `avail_results[date]` |
| Identity resolves | Set `identity_resolved=True`, populate `memory.last_slots` |
| `_transition("patient_found")` | Call `_cleanup_speculation()` |
| `_transition("no_match")` | Cancel pending `avail_tasks`, call `_cleanup_speculation()` |
| Dispatcher destroyed | Guard cleanup if not already done |

---

## 5. Cancellation Primitives

### 5.1 `asyncio.Task.cancel()` semantics

Throws `CancelledError` into coroutine on next event-loop cycle. Non-blocking.

### 5.2 Required drain pattern

```python
async def _cancel_and_drain(task: asyncio.Task) -> None:
    if task.done():
        return
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
        pass
```

### 5.3 Safety table

| Task | HTTP verb | Safe to cancel mid-flight? |
|---|---|---|
| `T_find` (GET) | GET | Yes — read-only |
| `T_avail_*` (GET) | GET | Yes — read-only |
| Pre-built `create_patient` payload (pure Python) | none | Yes — no I/O |
| Real `create_patient` (POST) | POST | NO — terminal |
| `create_appointment` (POST) | POST | NO — terminal |

This is why speculative `create_patient` must remain pre-validated payload only.

---

## 6. Idempotency Analysis

### 6.1 `find_patient_by_name_dob` — SAFE

HTTP GET, pure read.

### 6.2 `list_availability_slots` — SAFE

HTTP GET. Stale prefetch is advisory; create_appointment's partial unique index is the safety net for slot races.

### 6.3 `create_patient` — NOT SAFE as HTTP

Phone unique constraint only catches phone collisions. Two callers named "John Doe" with different phones would create two patient rows. **Speculative create_patient must be Python-only payload prep — no HTTP.**

This deliberately departs from the background-create pattern. Our EHR's POST /patients takes ~5ms; no latency cliff to hide.

---

## 7. Architecture: Why T_create Is Payload-Prep Only

The background-create pattern hides 5-7s of remote-EHR latency by firing background creates. Our EHR is ~5ms — sub-noise. Real wins come from:

1. `T_find` overlapping LLM greeting + caller first utterance (50-150ms).
2. `T_avail` prefetch overlapping IDENTIFY_PATIENT + CHOOSE_INTENT (50-200ms × 4 dates).

---

## 8. Fuzzy Match Disambiguation Protocol

### Similarity thresholds

| Similarity | Count | Classification | Action |
|---|---|---|---|
| >= 0.97 | 1 | Exact match | Proceed |
| 0.85 - 0.97 | 1 | Fuzzy single | Confirm before commit |
| 0.85+ | 2+ | Fuzzy multiple | List + choose |
| < 0.85 | any | No match | REGISTER_PATIENT |

`EXACT_THRESHOLD = 0.97` is the new constant. Tunable.

### Disambiguation flow

When fuzzy:
1. Do NOT call `_transition("patient_found")`.
2. Do NOT set `identified_patient`.
3. Inject synthetic tool-result into `self.history` with candidate list using `[N]` handles.
4. LLM asks caller to confirm/choose.
5. On affirmation, map selection to `fuzzy_candidates[i]`, set `identified_patient`, transition.

---

## 9. File-by-File Change List

### 9.1 NEW: `src/prosper/speculation.py`

```python
EXACT_THRESHOLD: float = 0.97

@dataclass
class SpeculativePatientPayload: ...

@dataclass
class SpeculationStore: ...

async def _cancel_and_drain(task, *, timeout=2.0) -> None: ...
async def cleanup_speculation(store: SpeculationStore) -> None: ...
def classify_find_result(patients) -> tuple[str, list]: ...
def build_disambiguation_message(candidates) -> str: ...
def next_n_business_days(from_date, n=4) -> list[date]: ...
```

### 9.2 MODIFY: `src/prosper/dispatcher.py`

- `SessionMemory.speculation: SpeculationStore | None = None` (new field, line 78).
- `_transition` → async, OR add `_pre_transition_hook(label)` async hook.
- `_maybe_transition_from_tool` → async. Insert `classify_find_result` + disambiguation injection before existing IDENTIFY_PATIENT branch.
- NEW: `async def _launch_speculation(self) -> None`. Launches `T_find` + 4× `T_avail` tasks.
- NEW: `def _on_avail_task_done(self, task, d)`. Callback merging avail result into `memory.last_slots` if empty.
- NEW: `def _consume_avail_prefetch(self) -> None`. Merge cache at CHOOSE_INTENT→BOOK_FLOW.
- `_maybe_transition_from_user_text` → async. Call `await self._launch_speculation()` at GREETING→go_identify.
- `handle_user_turn` → already async; needs `await` on new async children.

### 9.3 NO CHANGE: `src/prosper/flows.py`

FSM, ALLOWED_TOOLS, TRANSITIONS unchanged.

### 9.4 OPTIONAL: `src/prosper/tools.py`

`create_patient_handler` could take pre-validated payload — saves ~2ms. Skip unless cleanup is trivial.

### 9.5 NO CHANGE: `src/prosper/ehr_client.py`

CancelledError already propagates correctly through httpx.

### 9.6 NEW: `tests/test_speculation.py`
### 9.7 MODIFY: `evals/scenarios.py`

(See Section 10.)

---

## 10. Test Scenarios

### 10.1 Unit tests — `tests/test_speculation.py`

- `test_classify_find_result_exact` — 1 patient @ 0.99 → "found_exact".
- `test_classify_find_result_fuzzy_single` — 1 patient @ 0.90 → "found_fuzzy_single".
- `test_classify_find_result_fuzzy_multiple` — 2 patients @ 0.85+ → "found_fuzzy_multiple".
- `test_classify_find_result_no_match` — empty list → "no_match".
- `test_cancel_and_drain_cancels_task` — long sleeper cancelled within 2.5s.
- `test_cancel_and_drain_handles_already_done` — no-op on completed task.
- `test_cleanup_speculation_cancels_all` — 3 tasks all cancelled.
- `test_next_n_business_days_skips_weekend` — Friday → Mon-Tue-Wed-Thu next week.
- `test_cancel_get_request_is_safe` — httpx GET cancelled mid-flight; pool unaffected.

### 10.2 Dispatcher integration tests — `tests/test_dispatcher.py` additions

- `test_speculation_launches_on_identify_entry` — assert tasks exist after first user turn.
- `test_avail_prefetch_populates_last_slots_before_book_flow` — BOOK_FLOW first turn does NOT call list_availability_slots.
- `test_no_double_write_on_speculative_create` — POST /patients called exactly once.
- `test_cleanup_on_no_match_transition` — avail_tasks cancelled after no_match.
- `test_fuzzy_disambiguation_blocks_gate` — identified_patient stays None until affirmation.
- `test_fuzzy_multiple_candidates_prompt` — both candidates surfaced with [1], [2] handles.

### 10.3 Eval scenarios — `evals/scenarios.py` additions

- `speculative_exact_match_fast_booking` — assert `list_availability_slots` call count == 0.
- `speculative_fuzzy_single_confirm` — fuzzy single triggers explicit confirmation.
- `speculative_fuzzy_multiple_disambiguate` — caller picks from list.
- `speculative_no_match_avail_cancelled` — REGISTER_PATIENT path; create_patient called exactly once.
- `speculative_fuzzy_then_deny` — caller denies fuzzy match → bot collects DOB to disambiguate.

---

## 11. Failure Modes and Recovery

### FM-1: T_find errors
Detection: `is_err(store.find_result)`. Recovery: fall back to normal LLM find tool call. T_avail keeps running.

### FM-2: All T_avail tasks fail
Detection: all avail_results are Err. Recovery: silent; BOOK_FLOW calls list_availability_slots normally.

### FM-3: T_avail incomplete at BOOK_FLOW entry
Detection: `memory.last_slots` empty. Recovery: `_on_avail_task_done` writes when it completes if `last_slots` still empty. Invariant: never overwrite non-empty `last_slots`.

### FM-4: Multiple exact-name patients
Detection: `len(patients) > 1` even above EXACT_THRESHOLD. Recovery: treat as `found_fuzzy_multiple`. Never auto-pick `patients[0]`. (A common bug in naive implementations.)

### FM-5: cleanup_speculation hangs
Detection: `_cancel_and_drain` timeout (2s guard). Recovery: set `cleaned_up=True` and proceed. Orphan task has no shared mutable state (read-only EHR).

### FM-6: Caller corrects name mid-IDENTIFY
Detection: second utterance in IDENTIFY_PATIENT with `identity_resolved=False`. Recovery: cancel current T_find, re-launch with corrected name. Reset find_result/identity_outcome/fuzzy_candidates.

### FM-7: Race between T_find callback and LLM tool call
Detection: LLM calls find_patient_by_name_dob while T_find still in flight. Recovery: `_execute_tool` checks `store.find_result is not None` at top — short-circuits to cached result. `_record_tool_result` is the single write point — no double-write.

### FM-8: Prefetched slot taken at create_appointment time
Detection: create_appointment returns slot_taken_other_patient. Recovery: existing LLM flow handles it. Re-call list_availability_slots for fresh data.

---

## 12. Known Risks and Open Questions

- **R-1:** asyncio task management is new in dispatcher. Mitigation: isolate in `speculation.py`.
- **R-2:** `_transition` becoming async touches ~15 lines of call sites. Mechanical but wide.
- **R-3:** Two write paths to `memory.last_slots` (`_record_tool_result` and `_consume_avail_prefetch`). Invariant: only write when empty.
- **R-4:** Fuzzy disambiguation adds 1 user turn. Right UX trade-off (wrong patient is worse).
- **R-5:** 4× HTTP GETs per call on EHR. Mitigation: future scope — gate by intent likelihood.

---

## 13. Out-of-Scope

- No code changes in this pass.
- No prompt changes — `CLINIC_PERSONA` and `TASK_MESSAGES` stay as-is.
- No DB schema changes.
- No eval runner infrastructure changes.

---

## 14. Essential Files Reference

Absolute paths from repo root `C:\Users\matia\Desktop\prosperai\`:

- `src/prosper/dispatcher.py` — full session memory, FSM transitions, tool execution
- `src/prosper/flows.py` — ALLOWED_TOOLS, TRANSITIONS, State enum
- `src/prosper/tools.py` — handlers, HANDLERS dict
- `src/prosper/ehr_client.py` — EHRClient, _request, cancellation
- `src/prosper/ehr/api.py` — POST /patients (409 dup phone), GET /availability
- `src/prosper/ehr/models.py` — Patient.phone unique (line 74), Appointment partial index (113-120)
- `src/prosper/ehr/repository.py` — normalize_phone, find_patient_by_name_dob (token_sort_ratio)
- `evals/scenarios.py` — existing scenario structure
