# FUTURE.md — Prosper Health Voice Agent: Proposed Next Features

Priority-ranked improvements that would meaningfully strengthen this submission relative to the Prosper Technologies challenge brief. Items are ordered within each category from highest to lowest expected reviewer impact.

---

## 1. Reliability / Production-Readiness

### 1.1 Universal Goodbye-Intent Transition (Dispatcher)

**Why this matters to Prosper:** ERRORS.md identifies the missing `goodbye → END` universal transition as the root cause of ~80% of live-eval terminal-state failures — fixing it directly converts 10+ currently-failing live scenarios to passing, which is the single highest-leverage reliability change remaining.

- Add a `_detect_goodbye_intent(text)` helper in `dispatcher.py` that matches a broad pattern set (`bye`, `never mind`, `actually forget it`, `talk to you later`, `that's all`, etc.) using a short regex union.
- Call it at the top of `handle_user_turn`, before the per-state LLM dispatch, so any state can transition to `END` on a clear goodbye without burning an LLM call.
- Extend `TRANSITIONS` in `flows.py` — the `"goodbye"` edge already exists in every state; the issue is it is never triggered from user text in most states.
- Add three eval scenarios: `goodbye_from_book_flow`, `goodbye_from_cancel_flow`, `goodbye_from_register`, each with a persona that hangs up mid-flow without completing.

**Files to touch:** `src/prosper/dispatcher.py`, `src/prosper/flows.py`, `evals/scenarios.py`
**Effort:** S | **Risk:** low

---

### 1.2 STT/TTS Multi-Provider Fallback (ServiceSwitcher Stub)

**Why this matters to Prosper:** Interview question #6 asks directly about the reliability story; the current answer covers only LLM outages. ElevenLabs outages are a real production risk for a voice-first product. Pipecat issue #4139 is the upstream blocker, but a local shim can be done now.

- Define a `ServiceSwitcherProtocol` in `bot.py` with `primary` / `fallback` fields for both STT (ElevenLabs → Deepgram) and TTS (ElevenLabs Flash → OpenAI TTS).
- Wrap the Pipecat service construction in a factory that catches `TransportConnectionError` on the first frame and hot-swaps to the fallback service.
- Gate the fallback swap on a `PROSPER_STT_FALLBACK` / `PROSPER_TTS_FALLBACK` env var (unset = disabled) so the default path is unchanged.
- Add a unit test that mocks the primary service raising `TransportConnectionError` and asserts the fallback service is instantiated.

**Files to touch:** `src/prosper/bot.py`, `env.example`, `tests/test_bot.py`
**Effort:** M | **Risk:** med (Pipecat internals can change)

---

### 1.3 AvailabilityCache with 60-Second TTL

**Why this matters to Prosper:** `list_availability_slots` is the highest-frequency EHR read during a call (called once per date the caller asks about). Even at the current ~10 ms p50, caching removes it from the critical path entirely and demonstrates production thinking about hot-path reads.

- Add `AvailabilityCache` dataclass in `src/prosper/ehr/repository.py` holding a `dict[tuple[date, str | None], tuple[list[Slot], float]]` (key = date + optional provider_id, value = result + expiry timestamp).
- Wrap `list_availability_slots` in the repository with a `get_or_refresh` that checks the cache first; invalidate on `create_appointment` or `cancel_appointment` for the same date.
- Expose `PROSPER_AVAILABILITY_CACHE_TTL` env var defaulting to `60`; set to `0` to disable (tests should set it to `0` to stay deterministic).
- Add bench entry to `docs/bench-results.md` comparing cold vs warm p50.

**Files to touch:** `src/prosper/ehr/repository.py`, `env.example`, `docs/bench-results.md`, `tests/test_ehr.py`
**Effort:** S | **Risk:** low

---

## 2. Eval Quality

### 2.1 Adversarial Scenario Generator from Transcript Templates

**Why this matters to Prosper:** The challenge brief emphasises robust evaluation; the current 16 scenarios are hand-authored. A generator that produces novel adversarial variants would demonstrate a scalable eval discipline, not just a one-time test suite.

- Add `evals/generator.py` with a `ScenarioTemplate` dataclass: a scenario plus a list of `PerturbationRule` objects (e.g. swap the phone number with a malformed one, inject a refusal phrase mid-confirmation, replace the name with an injection string).
- Implement four concrete perturbation rules: `PhoneFormatChaos`, `NameInjection`, `MidFlowAbort`, `OffTopicProbe`.
- A `generate(template, rules) -> list[Scenario]` function returns concrete `Scenario` objects that can be dropped into the existing runner without changes.
- Add a `make gen-eval` target that generates and immediately runs the expanded set, printing a coverage summary of which FSM transitions were exercised.

**Files to touch:** `evals/generator.py` (new), `Makefile`, `evals/scenarios.py`
**Effort:** M | **Risk:** low

---

### 2.2 Multi-Model Judge with Disagreement Alerting

**Why this matters to Prosper:** ADR-003 justifies paired state + judge, but the judge is a single OpenAI call. A judge that disagrees with itself across two models on the same transcript is a stronger signal of a genuine edge case than a unanimous pass — directly strengthening the "eval quality" story for reviewers.

- Add a second judge call in `evals/judge.py` using a different model (e.g. `gpt-4o` as primary, `gpt-4o-mini` as secondary) with identical criteria.
- A `JudgeResult` now carries `primary_pass`, `secondary_pass`, and `agreed: bool`.
- A scenario only fails on `not agreed and not primary_pass`; disagreements are flagged in the CLI output as `WARN: judge disagreement` and written to a separate `evals/results/disagreements.jsonl` file for later review.
- Add `PROSPER_JUDGE_MODEL` / `PROSPER_JUDGE_SECONDARY_MODEL` env vars so the model pair is configurable.

**Files to touch:** `evals/judge.py`, `evals/types.py`, `evals/runner.py`, `env.example`
**Effort:** M | **Risk:** low

---

### 2.3 Golden-Trace Replay Mode

**Why this matters to Prosper:** Live-eval failures in ERRORS.md are largely persona-script mismatches. A golden-trace mode replays a recorded human transcript turn-by-turn through the dispatcher, asserting state transitions and tool calls match without involving the LLM — this is a deterministic regression test that costs zero tokens and catches dispatcher-logic regressions before the live eval run.

- Add `evals/trace_replay.py` with a `GoldenTrace` dataclass holding an ordered list of `(role, text, expected_state, expected_tool | None)` tuples.
- A `replay(trace, dispatcher) -> ReplayResult` feeds each user turn into `dispatcher.handle_user_turn` with the LLM mocked to the recorded assistant response, then asserts state and tool match.
- Ship three initial golden traces captured from the happy-path manual browser test.
- Add `make replay` target; it runs in under 1 second, no API key required.

**Files to touch:** `evals/trace_replay.py` (new), `evals/traces/` (new directory), `Makefile`
**Effort:** M | **Risk:** low

---

## 3. Voice UX / Latency

### 3.1 Streaming TTS via Flush-After-Clause

**Why this matters to Prosper:** Interview question #5 calls this "the biggest remaining perceived-latency win." The current architecture emits the full LLM response as a single TTS request; splitting on clause boundaries (`. `, `? `, `! `) allows ElevenLabs to start synthesizing the first clause while the second is still generating.

- Add a `ClauseStreamingProcessor` Pipecat processor in `src/prosper/bot.py` that buffers the LLM text stream and emits a `TTSTextFrame` on each sentence boundary.
- Detect boundaries with a small regex on `[.!?]\s` and a 120 ms stale-buffer flush timer for the trailing fragment.
- Instrument the clause-level TTFT separately in `TimingCollector` under `phase="tts_clause_1"` to measure actual improvement.
- Gate behind `PROSPER_STREAMING_TTS=1` env var so the default path (single-frame) is unchanged and no existing tests break.

**Files to touch:** `src/prosper/bot.py`, `src/prosper/observability/timing.py`, `env.example`, `tests/test_bot.py`
**Effort:** M | **Risk:** med (depends on Pipecat frame-pipeline internals)

---

### 3.2 Proactive Slot Pre-fetch on STT Partials

**Why this matters to Prosper:** During `BOOK_FLOW`, the caller says "I'd like something on Tuesday" and then pauses. Today the bot waits for the final `TranscriptionFrame`. If the partial already contains a date, the EHR availability query can fire immediately — shaving the full `list_availability_slots` round-trip from the perceived latency.

- Listen to `TranscriptionFrame(is_final=False)` in `DispatcherProcessor.process_frame` when `state == BOOK_FLOW`.
- Run a lightweight `_extract_date_from_partial(text) -> date | None` (regex + `dateutil` parse, no LLM call) on each partial.
- On a confident date extraction, fire `EHRClient.list_slots(date)` in the background; store the result under `SessionMemory.prefetched_slots`.
- In `_execute_tool("list_availability_slots")`, return `prefetched_slots` immediately if the requested date matches, bypassing the EHR call.
- Add a `phase="slot_prefetch"` timing span and a `prefetch_hit_rate` metric in `TimingCollector`.

**Files to touch:** `src/prosper/dispatcher.py`, `src/prosper/bot.py`, `src/prosper/observability/timing.py`, `tests/test_dispatcher.py`
**Effort:** M | **Risk:** med (partial text is noisy; needs a confidence threshold to avoid wasted EHR calls)

---

## 4. EHR Depth

### 4.1 Provider Preference Capture and Routing

**Why this matters to Prosper:** The current `BOOK_FLOW` offers all providers indiscriminately. A real clinic booking agent would remember "caller prefers Dr. Patel" within a session and filter availability without prompting — this is a concrete domain feature interviewers would notice is missing versus a real clinical product.

- Add `preferred_provider_id: str | None` to `SessionMemory` in `dispatcher.py`.
- Add a `set_provider_preference` tool (whitelist: `BOOK_FLOW`) that the LLM can call once the caller names a provider, storing the id in `SessionMemory`.
- Pass `provider_id=memory.preferred_provider_id` automatically to `list_availability_slots` calls when set, so the LLM never has to repeat the filter.
- Add an eval scenario `provider_preference_respected`: caller states "I always see Dr. Patel", books — assert `list_availability_slots` was called with the correct `provider_id`.

**Files to touch:** `src/prosper/dispatcher.py`, `src/prosper/tools.py`, `src/prosper/flows.py`, `evals/scenarios.py`
**Effort:** S | **Risk:** low

---

### 4.2 Appointment Notes / Reason-for-Visit Capture

**Why this matters to Prosper:** The EHR schema already has a `notes` field on `AppointmentCreate`; the bot never populates it. Capturing "reason for visit" is a standard clinical intake step — demonstrating it works end-to-end shows the EHR model was designed with real workflows in mind, not just demo scaffolding.

- Extend `CONFIRM_BOOK` state: after the LLM reads back the slot, it asks "Is there anything you'd like the doctor to know in advance?" and passes the answer as `notes` to `create_appointment`.
- Add `notes` to `SessionMemory.pending_notes` so the dispatcher passes it through without the LLM needing to re-state it.
- Cap at 500 chars (the existing `max_length` Pydantic constraint already enforces this at the EHR boundary).
- Add an eval scenario `reason_for_visit_captured`: persona says "I have a sore throat", assert the created appointment row has non-empty notes.

**Files to touch:** `src/prosper/dispatcher.py`, `src/prosper/prompts.py`, `evals/scenarios.py`
**Effort:** S | **Risk:** low

---

## 5. Security / Compliance

### 5.1 Immutable Tool-Call Audit Log with PII Redaction — **shipped 2026-05-20**

Implemented as the Operator Console's `AuditJSONLWriter`
(`src/prosper/console/audit.py`). Every `ConsoleEvent` published by the
dispatcher is appended to `data/audit/<session_id>.jsonl` by a
non-blocking drain task. PII redaction runs at the event boundary
(masked name + phone, year-only DOB), and the bus rejects any
`_masked` payload field that looks unredacted. Replayable via
`GET /console/replay/{session_id}`. See ADR 004.

Original proposal kept below for reference.

**Why this matters to Prosper:** SECURITY.md documents PII redaction in log lines but not a durable, structured audit trail of every tool invocation and outcome. For a HIPAA-adjacent product, an auditor needs "who called `cancel_appointment` for patient X at time T" — not just the absence of PII in journald.

- Add `src/prosper/observability/audit.py` with an `AuditEvent` dataclass: `ts`, `call_sid`, `state`, `tool_name`, `outcome_code`, `redacted_args` (args passed through `redact_pii` before storage).
- Append-only writes to a `data/audit.jsonl` file via a non-blocking `asyncio.Queue` drain loop (never blocks the call path).
- Expose `GET /audit?call_sid=&from=&to=` on the EHR for ops-tier queries; gate behind `PROSPER_AUDIT_ENABLED=1` so tests don't produce junk files.
- Add a unit test asserting that a `cancel_appointment` invocation produces an audit entry with `outcome_code` and no raw phone number in `redacted_args`.

**Files to touch:** `src/prosper/observability/audit.py` (new), `src/prosper/dispatcher.py`, `src/prosper/ehr/api.py`, `env.example`, `SECURITY.md`
**Effort:** M | **Risk:** low

---

### 5.2 Input Validation Hardening (OWASP A03) on EHR Endpoints

**Why this matters to Prosper:** The current Pydantic `max_length` caps prevent memory exhaustion, but there is no validation of phone number format, DOB plausibility (future dates, > 150 years ago), or appointment note content (script/HTML injection). A clinical EHR that stores unvalidated user input is an OWASP A03 finding.

- Add a `PhoneStr` custom Pydantic type in `src/prosper/ehr/schemas.py` that validates E.164 format (`^\+[1-9]\d{7,14}$`) with a clear error message.
- Add a `DobField` validator that rejects dates after today and before 1900-01-01.
- Strip HTML/script tags from `notes` and `reason` fields via a one-line `bleach.clean` (or a lightweight equivalent with no new deps — just `re.sub` on `<[^>]+>` is sufficient for this threat model).
- Update existing EHR tests to assert the 422 response body on invalid input.

**Files to touch:** `src/prosper/ehr/schemas.py`, `tests/test_ehr.py`, `SECURITY.md`
**Effort:** S | **Risk:** low

---

## 6. DX / Contributor Experience

### 6.1 Dispatcher Trace Viewer (CLI)

**Why this matters to Prosper:** ERRORS.md's fix path for the lone E1 crash begins "add `--debug` mode to `evals/runner.py` that prints `dispatcher.history` before each LLM call." This feature is that item, generalised: a single CLI command that renders the full dispatcher trace for any scenario in a readable table without requiring a live LLM call.

- Add `--trace` flag to `python -m evals` that, after running a scenario (mock or live), prints a formatted table: turn number, state, user text (redacted), tool called, tool result code, state transition.
- Pipe through `src/prosper/observability/redact.py` so no raw PII appears in terminal output.
- The table is derived from `Dispatcher.transcript` (already populated) — no new data collection needed.
- Add `make trace SCENARIO=new_patient_books` convenience target.

**Files to touch:** `evals/runner.py`, `evals/__main__.py`, `Makefile`
**Effort:** S | **Risk:** low

---

### 6.2 Scenario-from-Transcript Scaffolder

**Why this matters to Prosper:** CONTRIBUTING.md describes adding a scenario as a "20-line PR" but the 20 lines still require knowing the `StateExpectation` schema. A scaffolder that reads a raw transcript and outputs a populated `Scenario` stub lowers the barrier for clinical staff or QA to contribute new test cases without reading the codebase.

- Add `scripts/scaffold_scenario.py` that reads a plain-text transcript (one line per turn, format `USER: ...` / `BOT: ...`) from stdin or a file argument.
- Infer `StateExpectation` fields heuristically: presence of "booked" in a BOT line with a prior tool call implies `booked_appointment_count_delta=1`; "cancelled" implies `cancelled_appointment_count_delta=1`.
- Output a Python snippet ready to paste into `evals/scenarios.py`, with `# TODO:` comments on fields it couldn't infer.
- Document in `CONTRIBUTING.md` as the recommended first step when a manual call reveals unexpected behaviour.

**Files to touch:** `scripts/scaffold_scenario.py` (new), `CONTRIBUTING.md`
**Effort:** S | **Risk:** low
