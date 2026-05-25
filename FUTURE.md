# FUTURE.md — Prosper Health Voice Agent: Proposed Next Features

Priority-ranked improvements that would meaningfully strengthen this submission relative to the Prosper Technologies challenge brief. Items are ordered within each category from highest to lowest expected reviewer impact.

---

## 1. Reliability / Production-Readiness

### 1.1 Universal Goodbye-Intent Transition (Dispatcher) — ✅ SHIPPED

Landed. `flows.py` carries a `"goodbye"` edge on every non-END state and
`dispatcher.py` has a tiered goodbye matcher (`_GOODBYE_HARD` + trailing-anchor
tiers). Adversarial regression on both directions:
`tests/adversarial/test_confirm_goodbye.py` (true-positive) +
`test_goodbye_false_positive.py` (F-012 mid-utterance "bye"). Abandon scenarios
(`goodbye_at_*`, `reschedule_abort_at_confirm`) cover hang-up at every reachable
state.

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

### 1.3 AvailabilityCache (server-side, opt-in, default-off)

**Status:** deferred-by-design (see `ARCHITECTURE.md` §16.1). Spec below is the
adjudicated design (two independent research passes + an LLM council, 2026-05-25)
so it can be built correctly in one sitting when the EHR moves remote. **Not yet
built** — on the current in-process SQLite a slot read is ~10 ms, so a cache
saves noise; the design only pays off against a remote EHR (100–300 ms).

**Why this matters to Prosper:** the availability read is the highest-frequency
EHR call during a booking. Caching it demonstrates hot-path thinking — but only
honestly, with correct invalidation, which is the entire difficulty.

**The two corrections to the naive spec (both are latent bugs):**
1. The real repository symbol is **`list_available_slots`** (not
   `list_availability_slots` — the root `CLAUDE.md`, `ehr/CLAUDE.md`, and older
   text miscall it; fix those references in the same change, rule 11).
2. The cache key MUST be the **full 4-tuple**
   `(date, provider_id, specialty_lowercased, duration_minutes)`. A
   `(date, provider_id)` key aliases a 30-min query with a 90-min one and an
   unfiltered query with a `specialty="Therapist"` one → wrong results.
   (`specialty` is lowercased because the query matches `ilike`.)

**Placement — server-side, inside the repository (NOT the API edge, NOT the
bot):**
- Live inside `repository.py`. Populate in `list_available_slots`; **evict inside
  the three write functions (`create_appointment`, `cancel_appointment`,
  `reschedule_appointment`) immediately after a successful `commit()`** so
  invalidation is co-located with the mutation and structurally cannot be
  forgotten. A decorator or endpoint-edge cache can't see *which* dates changed
  and silently misses the hard cases below.
- Store **serialized `SlotOut` dicts, not ORM `Slot` objects** — `Slot` is
  session-bound and `_slot_to_out` lazy-loads `provider.name`; reusing a detached
  instance raises `DetachedInstanceError`.
- **Why not bot-side (`EHRClient`)?** That's the *only* placement that beats a
  remote HTTP round-trip, but it's **unsafe across processes**: a second bot, the
  front desk, or another call mutates the DB with no invalidation signal reaching
  the bot's cache → stale forever. The safe server-side cache only removes the
  EHR's internal ~10 ms DB read; the version that would actually beat remote
  latency is the unsafe one. That tension is exactly why this is deferred, not
  shipped.

**Invalidation (the real hazard is a *hidden freed slot*, not a stale free one):**
| Write | Date(s) to evict |
|---|---|
| `create_appointment` | the booked slot's date — and for 60/90-min visits, **every chained slot's date** |
| `cancel_appointment` | the freed appointment's slot date |
| `reschedule_appointment` | **two** dates — old slot date AND new slot date |

A stale *"slot still free"* hit is **safe**: the DB partial-unique index returns
`409 slot_taken` and the bot re-offers (the cache is advisory, never the
authority). A stale *"freed slot hidden"* hit has **no DB guard** — hence
eviction on cancel/reschedule is mandatory.

**Config + TTL:**
- `ENABLE_AVAILABILITY_CACHE` (default `false` — ship opt-in; on local SQLite
  it's a no-op, only enable when the EHR is remote).
- `PROSPER_AVAILABILITY_CACHE_TTL` seconds (default `10`; `0` = disabled). Short
  because a caller who just cancelled should see the freed slot almost
  immediately; TTL is only a backstop behind explicit eviction. Tests set `0` for
  determinism (the `cutoff = now()` intra-day decay already makes warm vs cold
  results differ across slot boundaries).

**Production upgrade path (FUTURE-of-FUTURE):** multi-process needs a shared
cache (Redis) with a pub/sub `availability-evict` topic broadcasting affected
`(provider, date)` on every write. **Do not implement a bot-side cache without
that channel.**

**Files to touch:** `src/prosper/ehr/repository.py`, `src/prosper/ehr/api.py`
(wire the enable flag), `env.example`, `docs/bench-results.md` (cold vs warm
p50), `tests/ehr/test_repository.py` (eviction-on-write + 409-still-fires +
freed-slot-reappears), and the `list_availability_slots`→`list_available_slots`
doc-name fixes.
**Effort:** S–M | **Risk:** low (opt-in, default-off, DB remains the guard)

---

## 2. Eval Quality

### 2.1 Adversarial Scenario Generator from Transcript Templates — ✅ SHIPPED

Landed (commit `831b99a`). `evals/generator.py` + `make gen-list` / `make
gen-eval` targets.

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

### 2.3 Golden-Trace Replay Mode — ✅ SHIPPED

Landed (commit `bfb3a70`). `evals/trace_replay.py` + `make replay` /
`make replay-record`.

### 2.4 Audio-path regression coverage (barge-in / cutoff) — ✅ SHIPPED (offline tier)

The challenge deliverable lists *"automated eval suite (LLM-as-judge over
scripted scenarios + **smoke audio tests**)"*. The audio path splits into two
tiers; the high-value one — the interruption / call-cutoff behaviour that is
**most noticeable to a caller** (bot won't stop when interrupted; a half-said
utterance fires after the line drops) — is now covered **offline, $0, in
`make verify`**.

**Shipped (`tests/test_barge_in_pipeline.py`, commit pending):** a deterministic
frame-injection integration test that drives `DispatcherProcessor` +
`TTSAudibleObserver` with Pipecat frames in pipeline order — no live audio, no
API keys. It closes the gap that `test_barge_in.py` (dispatcher method in
isolation) and `test_observers.py` (observer with a stub callback) left open:
the *propagation* observer→real-dispatcher, and the aggregation timer under
interruption / hang-up. Covers: (a) barge-in mid-turn truncates + marks the real
`history[-1]`; (b) a spurious between-turns interrupt does NOT clobber a
fully-spoken turn; (c) `EndFrame` (hang-up) cancels the pending aggregation so no
stale turn fires; (d) an interrupt mid-utterance does NOT drop the caller's
buffered barge-in words (guards against the tempting-but-wrong "cancel agg on
interrupt"). This last one confirmed the current behaviour is correct — no code
fix needed.

**Still deferred — the acoustic round-trip (Tier 3):** synth caller utterance
(ElevenLabs TTS) → bot pipeline → STT → dispatcher → TTS → caller-side STT → LLM
judge. Only this tier catches STT mis-transcription (TTS says "nine PM", STT
hears "nine AM"). Needs ElevenLabs credits + a live STT WebSocket replay (or
recorded WAV fixtures), so it stays `-m audio`, nightly/pre-deploy, out of
`make verify`. See `ARCHITECTURE.md` §16 / §17 #5. The VAD-tuning tier (real
`InputAudioRawFrame` through Silero) sits between the two and is also deferred.

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

### 3.3 Speculative execution on call start — *the latency answer (deferred, by design)*

> **This is the canonical answer to "what do we do about latency?"** (see
> `ARCHITECTURE.md` §15.1). On call start, overlap backend I/O with the caller's
> speech instead of doing it sequentially on demand: warm the identity lookup +
> the caller's upcoming appointments the moment a name is heard, and pre-stage
> the three intent branches (book/cancel/reschedule) in parallel. For a new
> caller, prepare the registration payload speculatively — but **never**
> speculatively `create_patient` (no name-dedupe → duplicate-patient risk).
> **Status: NOT building yet.** On local SQLite the gain is <200 ms vs real
> asyncio cancellation complexity; revisit when the EHR is remote (100–300 ms
> round-trips). **Scope — light warm-path vs full 3-branch race — to be decided
> by an LLM-council pass before any implementation.**

**Why this matters to Prosper:** On call start the bot asks the caller's name, then runs identity lookup and (later) availability sequentially. A speculative race — fire `find_patient_by_name_dob` and `list_availability_slots` for the next few business days in parallel the moment the name is heard — overlaps EHR I/O with the caller's speech (the MarioW333 pattern). Full design + sequence diagrams + cancellation discipline in `docs/research/speculative_race.md`.

**Shipped already (2026-05-23):** the *fuzzy disambiguation* half — `src/prosper/speculation.py` (`classify_find_result`, `build_disambiguation_message`, `EXACT_THRESHOLD`, `next_n_business_days`) plus dispatcher wiring. When a name+DOB lookup returns more than one candidate the bot now holds in `IDENTIFY_PATIENT`, reads the numbered candidates back, and resolves on the caller's pick (`pending_identity_candidates` + `_resolve_pending_identity`) instead of silently guessing. Name-first greeting shipped in the same pass.

**Deferred (the async prefetch itself):** on the current in-process SQLite EHR a lookup is ~30 ms, so racing it saves <200 ms while adding asyncio task-lifecycle + cancellation complexity in the dispatcher core. Revisit when the EHR moves out-of-process / remote (round-trips in the 100–300 ms range), where the overlap pays for the complexity. `next_n_business_days` is already in `speculation.py` ready for the availability fan-out.

- Launch `T_find` + N×`T_avail` as `asyncio.Task`s on the GREETING→IDENTIFY transition; cache results in a `SpeculationStore` on `SessionMemory`.
- Short-circuit `_execute_tool` for find/availability when a cached result exists; drain+cancel pending tasks on `no_match` / call end via a `_cancel_and_drain` helper (2 s guard).
- `create_patient` stays payload-prep only (no speculative HTTP) — our EHR has no name-based dedupe, so a speculative write could create a duplicate patient.

**Files to touch:** `src/prosper/speculation.py`, `src/prosper/dispatcher.py`, `tests/test_speculation.py`, `evals/scenarios.py`
**Effort:** L | **Risk:** med (asyncio cancellation discipline; see design doc §5, §11)

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

### 6.1 Dispatcher Trace Viewer (CLI) — ✅ SHIPPED

Landed (commit `6f1d6e8`). `--trace` flag on `python -m evals` + `make trace
SCENARIO=…`.

---

### 6.2 Scenario-from-Transcript Scaffolder — ✅ SHIPPED

Landed (commit `4a2cda0`). `scripts/scaffold_scenario.py`.
