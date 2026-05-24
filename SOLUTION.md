# Prosper Health voice agent — solution

> Reviewer-facing tour of the codebase as it stands today. Pair this with
> `docs/architecture.md` (process + FSM diagrams) and `docs/adr/001..004`
> (load-bearing decisions). Anything not covered here lives in `CLAUDE.md`
> (rules) or `CONTRIBUTING.md` (recipes).

## 1. Overview

A voice agent that books, reschedules, and cancels appointments at a
fictional US clinic over WebRTC. Two processes share one SQLite database:

- **`bot.py`** — Pipecat pipeline (ElevenLabs Flash v2.5 STT/TTS, OpenAI
  LLM) wired to a custom FSM dispatcher.
- **`src/prosper/ehr/api.py`** — FastAPI EHR backed by SQLite/SQLAlchemy.

The bot talks to the EHR over HTTP via `EHRClient` (an `httpx.AsyncClient`).
Tests and evals mount the EHR in-process through `httpx.ASGITransport` — no
socket, no port binding, fully hermetic.

## 2. Quick start

```bash
cp env.example .env       # add ELEVENLABS_API_KEY and OPENAI_API_KEY
make install              # uv sync
make seed                 # one-time
make ehr                  # terminal 1 — FastAPI on :8000
make bot                  # terminal 2 — Pipecat on :7860
# open http://localhost:7860 → Connect
```

Docker variant: `docker-compose up`.

## 3. Quick evaluate

```bash
make mock-eval            # zero-cost: all scripted scenarios via deterministic mock in ~5 s, no API key
PROSPER_EVAL_LIVE=1 OPENAI_API_KEY=… make eval            # live: full suite, paired state + judge
PROSPER_EVAL_LIVE=1 OPENAI_API_KEY=… uv run python -m evals --json evals/results/baseline.json
PROSPER_EVAL_LIVE=1 OPENAI_API_KEY=… uv run python -m evals --concurrency 4 --baseline evals/results/baseline.json
```

Gating is intentional: a missing `PROSPER_EVAL_LIVE=1` OR missing
`OPENAI_API_KEY` exits with code 2 so an account without quota fails loud
rather than silently passing. `make mock-eval` is the canonical offline
smoke test.

## 4. Process topology

```
[browser WebRTC]
       │
       ▼
   bot.py  (Pipecat pipeline + DispatcherProcessor)
       │  httpx
       ▼
ehr/api.py (FastAPI)  ──►  data/ehr.db  (SQLite + WAL)
       ▲
       │  ASGITransport (in-process, evals + tests only)
       │
  evals/runner
```

The HTTP loopback adds ~1 ms; it is dwarfed by STT/LLM/TTS, so keeping the
EHR as a separate process is free in latency and earns realism — reviewers
can hammer it independently, and it scales / fails independently from the
bot. See `docs/adr/002-separate-ehr-process.md`.

## 5. FSM

`src/prosper/flows.py` is plain data: a `State` enum, an `ALLOWED_TOOLS`
dict, a `TRANSITIONS` map. No framework. The dispatcher owns the runtime.

States today (11):

```
GREETING
  ↓ go_identify / goodbye
IDENTIFY_PATIENT  ──no_match──►  REGISTER_PATIENT
  ↓ patient_found                       ↓ registered
CHOOSE_INTENT  ◄──────────────────────────┘
  │
  ├─ wants_book ─────► BOOK_FLOW ─slot_chosen──► CONFIRM_BOOK ─booked──► END
  │                                                  ↑ abort
  ├─ wants_cancel ────► CANCEL_FLOW ─appt_chosen──► CONFIRM_CANCEL ─cancelled──► END
  │                       │                              │ cancelled_then_rebook
  │                       └─ nothing_to_cancel ──► END   └──► BOOK_FLOW
  │                                                  ↑ abort
  ├─ wants_reschedule ─► RESCHEDULE_FLOW ─slot_chosen─► CONFIRM_RESCHEDULE ─rescheduled──► END
  │                       │                              ↑ abort
  │                       └─ nothing_to_reschedule ──► END
  └─ goodbye ─► END                                 (every non-END state has a goodbye edge)
```

Per-state tool whitelist enforced by the dispatcher (not by prompt
instructions — the LLM literally cannot see tools outside the whitelist):

| State | Allowed tools |
|---|---|
| GREETING | (none) |
| IDENTIFY_PATIENT | `find_patient_by_phone`, `find_patient_by_name_dob` |
| REGISTER_PATIENT | `create_patient` |
| CHOOSE_INTENT | `route_intent` (internal — dispatcher-intercepted, no EHR call) |
| BOOK_FLOW | `list_availability_slots`, `suggest_specialty` |
| CANCEL_FLOW | `get_upcoming_appointments` |
| RESCHEDULE_FLOW | `get_upcoming_appointments`, `list_availability_slots` |
| CONFIRM_BOOK | `create_appointment` |
| CONFIRM_CANCEL | `cancel_appointment` |
| CONFIRM_RESCHEDULE | `reschedule_appointment` |
| END | (none) |

`route_intent` is whitelisted in CHOOSE_INTENT but **dispatcher-intercepted**
(`INTERNAL_TOOLS` in `flows.py`, `Dispatcher._handle_route_intent`) — it is the
hybrid-navigation tool: the LLM proposes the caller's intent
(`wants_book` / `wants_cancel` / `wants_reschedule`) and the dispatcher validates
the edge. It has no `HANDLERS` entry and fires no EHR call. `suggest_specialty`
(BOOK_FLOW) is a real handler backed by the triage mini-LLM (ADR 005, §6); its
`medical_emergency` Err drives the hard `BOOK_FLOW → END` edge so the booking
tools are physically unmounted on a red flag (audit F-011).

If the LLM tries a tool outside the whitelist, the dispatcher records a
`tool_rejected` transcript entry and feeds a synthetic error
`role:tool` message back into the LLM's history so the model retries on
the next inner iteration. Forbidden calls never reach an HTTP boundary.

See `docs/adr/001-hybrid-fsm-with-tool-whitelist.md` for the design
choice; `docs/architecture.md` for the diagram.

## 6. Tools

Nine EHR-backed handlers in `src/prosper/tools.py` (the `HANDLERS` map), plus
`route_intent` — whitelisted but dispatcher-intercepted, no handler, no EHR call
(see §5). All handlers return `Result[Ok[dict], Err]` (`src/prosper/result.py`).
The `Err.code` strings are **public eval contract** — scenarios assert on them,
so renaming a code is a breaking change and must update `evals/scenarios.py` in
the same commit.

| Tool | Whitelisted in | Err codes |
|---|---|---|
| `route_intent` *(internal — no handler)* | CHOOSE_INTENT | — (dispatcher validates the FSM edge) |
| `find_patient_by_phone` | IDENTIFY | `ehr_error` |
| `find_patient_by_name_dob` | IDENTIFY | `dob_unparseable`, `ehr_error` |
| `create_patient` | REGISTER | `dob_unparseable`, `patient_exists`, `ehr_error` |
| `suggest_specialty` | BOOK_FLOW | `medical_emergency`; passes through `llm.classify_symptoms`: `triage_unavailable`, `unknown_specialty`, `invalid_duration` |
| `list_availability_slots` | BOOK_FLOW, RESCHEDULE_FLOW | `date_unparseable`, `invalid_duration`, `ehr_error` |
| `create_appointment` | CONFIRM_BOOK | `missing_slot_id`, `missing_patient_id`, `hallucinated_slot_id`, `patient_id_mismatch`, `slot_taken_other_patient`, `no_consecutive_slots`, `patient_or_slot_not_found`, `ehr_error` |
| `get_upcoming_appointments` | CANCEL_FLOW, RESCHEDULE_FLOW | `ehr_error` |
| `cancel_appointment` | CONFIRM_CANCEL | `missing_appointment_id`, `hallucinated_appointment_id`, `appointment_not_found`, `ehr_error` |
| `reschedule_appointment` | CONFIRM_RESCHEDULE | `missing_appointment_id`, `missing_slot_id`, `hallucinated_appointment_id`, `hallucinated_slot_id`, `slot_taken_other_patient`, `appointment_or_slot_not_found`, `ehr_error` |

`suggest_specialty` maps a free-form symptom string → `{specialty,
duration_minutes, confidence, follow_up}` via the triage mini-LLM
(`llm.classify_symptoms`, a single `gpt-4o-mini` JSON-mode call). A red flag
returns `Err(medical_emergency)`; visit duration ∈ {30, 60, 90} flows into
`list_availability_slots`/`create_appointment` (multi-slot lock). Full design:
ADR 005.

Three things to know about the tool layer:

1. **`reschedule_appointment` is atomic.** Single transaction, single
   `PATCH` to `/appointments/{id}/slot`. Replaces the older
   cancel-then-rebook chain — if the new slot is held, the original
   appointment is preserved (no orphan window). The dispatcher chains
   the legacy `cancelled_then_rebook` path only when the caller flipped
   intent *mid-cancel* (e.g. said "reschedule" while already inside
   CANCEL_FLOW). Plain "reschedule" from CHOOSE_INTENT routes straight
   to the atomic path.

2. **`list_availability_slots` has a 6-day forward-scan fallback.** When
   the asked date is empty, the handler probes day+1..day+6 with the
   same specialty filter and returns the first hit under
   `next_day_with_slots`. The dispatcher transitions to CONFIRM_BOOK on
   either path and `_resolve_memory_handles` validates against the
   fallback slots so `create_appointment` doesn't reject them as
   hallucinated. Scenario `availability_falls_through_to_next_day`
   pins this behaviour.

3. **`specialty` filter.** `list_availability_slots` accepts an
   optional `specialty` string ("Therapist", "Dermatologist",
   "Psychiatrist", "General Practice", "Physiotherapist"). Today the
   LLM picks the string from caller intent; a mini-LLM router that
   maps complaint → specialty is **in flight** (§14). Scenarios
   `specialty_filter_therapist`, `specialty_unknown_falls_back`, and
   `specialty_no_filter_any_doctor` cover the three branches.

## 7. Dispatcher mechanics

`src/prosper/dispatcher.py` is intentionally small (~1200 LOC) so a
reviewer can read the whole machine in one sitting. Five things it
does beyond the FSM:

1. **Per-state LLM request.** `_messages_for_llm` returns
   `[system: CLINIC_PERSONA, system: TASK_MESSAGES[state], …history]`.
   Persona stays stable across turns (prompt-cache target); task
   message swaps per state.

2. **Handle redaction (audit A3).** `_redact_for_llm` strips raw UUIDs.
   Lists are enumerated `[1] 10:00 with Dr. Patel; [2] 10:30 …`. The
   LLM passes the bracketed number back as `slot_id` / `appointment_id`
   and `_resolve_memory_handles` swaps it for the real UUID before any
   HTTP call. The LLM has never seen and cannot read aloud an internal
   id.

3. **Session memory.** `SessionMemory.{identified_patient, last_slots,
   last_upcoming_appointments, wants_reschedule}`. Every tool response
   that returns a list updates the corresponding memory field;
   `_validate_against_memory` then rejects any write-tool call whose
   `slot_id` / `appointment_id` is not in that memory
   (`hallucinated_slot_id`, `hallucinated_appointment_id`). This is
   defence-in-depth on top of the EHR's own constraints.

4. **History sliding window + orphan pruning.** `_HISTORY_WINDOW = 40`.
   When the window slices off an assistant message that announced a
   `tool_call_id`, any `role:tool` reply whose id no longer has a
   parent is pruned in `_prune_orphan_tool_messages`. Without this,
   OpenAI rejects the next turn with
   `messages with role tool must be a response to a preceding message
   with tool_calls` (see `ERRORS.md` E1).

5. **Per-turn tool dedup.** A misbehaving model can fire the same tool
   with identical args repeatedly. After two duplicate calls in one
   turn the dispatcher injects a `BLOCKED:` synthetic response telling
   the model to speak to the user instead. Inner-loop budget is 4
   iterations — exhaustion drops a `FALLBACK_LINES["llm_loop_exhausted"]`
   line so the caller never hears dead air.

`bot.py` wires the dispatcher into Pipecat via `DispatcherProcessor`,
which consumes `TranscriptionFrame` directly (the default
`LLMUserAggregator` adds a 1 s aggregation tax we don't want; see the
note at the top of `bot.py`). A last-resort `try/except` around
`handle_user_turn` keeps a mid-call exception from killing the WebRTC
session — the caller hears "sorry, I missed that" and the dispatcher
recovers.

## 8. Operator console

`src/prosper/console/` ships a live operator pane fed by the dispatcher.
Nine event types from `events.py`:

`state_change`, `transcript_turn`, `tool_call_start`, `tool_call_end`,
`latency_tick`, `patient_identified`, `slots_offered`, `outcome`,
`turn_interrupted`.

Wiring:

- `dispatcher._publish(type, payload)` is a fire-and-forget call against
  an injected `ConsoleBus`. When no bus is passed (tests / eval runner)
  every publish site is a hard no-op.
- The bus uses bounded queues with overflow-drop semantics so a slow SSE
  client cannot back-pressure the call path.
- `_redact_tool_args` and `mask_name` / `mask_phone` enforce PII redaction
  on every payload before it hits the bus — the operator UI sees masked
  values, the transcript and audit log keep originals.
- `_publish_outcome` distinguishes `booked` / `cancelled` / `refused`
  (offer made and declined) / `abandoned` (caller dropped pre-confirm).
  Confusing the last two poisons clinic dashboards, so the categorisation
  is explicit.

Design spec: `docs/superpowers/specs/2026-05-20-operator-console-design.md`
+ `docs/adr/004-operator-console-event-stream.md`.

## 9. EHR

`src/prosper/ehr/`:

- `models.py` — SQLAlchemy: `Patient`, `Provider`, `Slot`, `Appointment`.
  Two load-bearing constraints:
  - **`Provider.specialty`** column (auto-migrated on startup since
    commit `3266b02` — see `db.py`).
  - **Partial unique index** on `Appointment(slot_id) WHERE
    status='scheduled'`. Concurrent booking races surface as `409
    slot_taken` (never 500), and a recoverable Err code propagates up.
- `repository.py` — single LEFT-OUTER-JOIN to list availability (was N+1
  pre-fix; bench dropped from 115 ms → ~10 ms — see
  `docs/bench-results.md`).
- `schemas.py` — Pydantic with `Field(max_length=…)` caps on every
  string field so a malicious POST can't load a multi-megabyte string
  into SQLite via the request parser.
- `api.py` — FastAPI app. Endpoint mapping vs the challenge spec:

| Challenge name | REST endpoint |
|---|---|
| `create_patient` | `POST /patients` |
| `find_patient` (by name + DOB) | `GET /patients/by-name-dob?name=&dob=` |
| `find_patient` (by phone — our addition) | `GET /patients/by-phone?phone=` |
| `list_availability_slots` | `GET /availability?date=&provider_id=&specialty=` |
| `create_appointment` | `POST /appointments` |
| `cancel_appointment` | `POST /appointments/{id}/cancel` |
| reschedule (atomic) | `PATCH /appointments/{id}/slot` |
| (helper) patient appointments | `GET /patients/{id}/appointments` |
| health | `GET /health` |

**Slot datetimes are naive UTC** (seed converts NY-local → UTC → strips
tzinfo). Any new code reading `slot.start_at` must follow the same
convention; mixing naive-local with naive-UTC silently misreads
availability.

**Past slots are filtered server-side** (`repository.list_availability_slots`)
so the bot cannot offer 8 am at 11 am.

## 10. Prompts and persona

`src/prosper/prompts.py`:

- `CLINIC_PERSONA` (~1100 tokens) — long on purpose. OpenAI's prompt
  cache kicks in at 1024+ stable tokens; the dispatcher reports
  `cached_prompt_tokens` per turn so the eval runner can show
  `cache=XX%` per scenario. **Do not trim** the persona to chase token
  cost; you'd lose more from a missed cache hit than you'd save.
- `TASK_MESSAGES[state]` — ≤ 1 KB per entry, enforced by
  `test_each_task_message_under_1_kb`. Bot guidance for the state lives
  here; the dispatcher injects today's date so the model can resolve
  "tomorrow" / "next Tuesday".
- `STATE_FILLERS` — short "one moment" lines pushed as a TTS frame
  before a tool-firing state so the caller never hears silence during
  the LLM round-trip.
- `FALLBACK_LINES["llm_loop_exhausted"]` — last-resort speech when the
  4-iteration inner loop exhausts without a real reply.

All caller-audible strings live here. Inline voice strings in `bot.py`
or `dispatcher.py` are a regression — they bypass the 1 KB gate and
fragment the cache.

## 11. Eval suite

Runner: `evals/__main__.py` + `evals/runner.py`. Each scenario asserts
on **both**:

- **State delta (deterministic).** DB row counts before/after, expected
  tool codes fired, forbidden tools didn't, FSM reached
  `expected_terminal_state`, and a regex post-check for hallucinated
  confirmations ("I've cancelled" with no matching `tool_ok`).
- **LLM judge (semantic).** Full transcript scored against
  natural-language `judge_criteria`. PASS only if both checks pass.

The paired check exists because the judge alone marked some adversarial
scenarios as PASS even when the bot said "I've cancelled that" without
any `tool_ok`. See `docs/adr/003-paired-state-and-judge-eval.md`.

### Scenarios

`evals/scenarios.py` defines 63 scenarios across these tag buckets:

| Tag | Scenarios | What's tested |
|---|---|---|
| happy | `new_patient_books`, `existing_patient_cancels`, `cancel_picks_from_list`, `caller_volunteers_email_at_registration`, `availability_falls_through_to_next_day`, `reschedule_existing_appointment`, `reschedule_multi_appointment_picks_second`, `specialty_no_filter_any_doctor`, `specialty_filter_therapist` | Mandatory flows + the "everything went right" reschedule and specialty paths |
| recovery | `dob_misheard_then_corrected`, `patient_correction_mid_register`, `phone_correction_mid_register`, `dob_unparseable_then_recovery` | Caller corrects a field mid-turn; bot must adopt the latest value |
| edge | `slot_taken_by_other`, `cancel_when_nothing_to_cancel`, `phone_format_chaos`, `slot_handle_out_of_range`, `availability_falls_through_to_next_day`, `goodbye_at_*` | Boundary behaviour: race, empty list, parsing chaos, dispatcher handle guard |
| adversarial | `prompt_injection_direct_override`, `prompt_injection_stored_in_name`, `cross_patient_cancel_refusal`, `hallucinated_confirmation_trap`, `multi_turn_drift_hallucinated_slot`, `off_topic_steering_and_budget`, `rude_caller_still_completes_booking`, `insurance_question_redirect`, `hallucinated_appointment_id_in_reschedule`, `reschedule_cross_patient_refusal` | Injection, authorization, hallucination, tone, scope |
| abandon | `goodbye_at_greeting`, `goodbye_at_identify`, `goodbye_at_choose_intent`, `goodbye_mid_confirmation`, `reschedule_abort_at_confirm`, `goodbye_at_book_flow`, `goodbye_at_cancel_flow`, `goodbye_at_reschedule_flow` | Caller hangs up at every reachable state (incl. mid-book/cancel/reschedule) — no orphan writes, no fake confirmations |
| reschedule | `reschedule_existing_appointment`, `reschedule_no_upcoming_appointments`, `reschedule_cross_patient_refusal`, `reschedule_abort_at_confirm`, `reschedule_multi_appointment_picks_second`, `hallucinated_appointment_id_in_reschedule` | Atomic move + every refusal/abort path on it |
| specialty | `specialty_unknown_falls_back`, `specialty_no_filter_any_doctor`, `specialty_filter_therapist` | Filter pass-through, unknown specialty graceful decline, no-filter wildcard |
| hybrid | `ambiguous_intent_routed_via_tool`, `route_intent_resolves_to_cancel`, `route_intent_resolves_to_reschedule`, `cancel_then_rebook_intent_flip` | The `route_intent` navigation tool (CHOOSE_INTENT) end-to-end + the cancel→rebook intent-flip chain |
| identity | `identify_by_name_dob_disambiguation` | Two fuzzy candidates → caller's pick resolves to the correct patient (F-013 class, end-to-end) |

Adding a scenario is a ~20-line PR per `CONTRIBUTING.md` — pure data
(`Scenario` dataclass), no framework changes.

### Mock-LLM mode (`make mock-eval`)

`evals/mock_llm.py` is a deterministic ~1700 LOC harness:

- `MockDispatcherLLM` plays a per-scenario script of `LLMReply` objects;
  it knows about dispatcher `__use_first_slot__` / `__use_upcoming_n__`
  sentinels so the script doesn't have to hard-code UUIDs.
- `MockPersonaLLM` replays canned caller utterances.
- `mock_judge_transcript` returns PASS unconditionally — mock mode tests
  the *runner*, not the judge.
- A `_END_NOW_MARK` sentinel force-transitions the dispatcher into
  `State.END` for refusal scenarios (no tools fire, no natural END
  transition). The real bot reaches END via `cancelled` / `booked` /
  `nothing_to_*` / `goodbye`.

Runs the whole suite in ~5 s offline. CLI flags:

- `--concurrency N` (default 4) — per-scenario isolated SQLite engine,
  cuts wall-clock ~3× without crossing transactional state.
- `--baseline previous.json` — fails the run with exit 3 if a
  previously-passing scenario regressed.
- `--mock-llm` — swap in the deterministic mock LLM.
- `--only NAME` — run a single scenario.

### Catching hallucinations without hand-dialing (eval strategy)

The challenge asks specifically for *ways to automatically test or simulate
calls so hallucinations and agent mistakes are caught without dialing in by
hand*. The field converges on one shape — a **synthetic caller** (LLM with
persona + goal) talks to the agent, scored by **deterministic state/tool
assertions + an LLM judge** — and the deterministic half must stay the hard
gate, because LLM-simulated callers drift off-goal (arXiv *"Lost in
Simulation"*). That is exactly the paired `state_pass AND judge_pass` design
already in place (ADR 003).

We organise the work as a cost-tiered pyramid; the right-hand column is where
this repo sits:

| Tier | Method | Cost | Status here |
|---|---|---|---|
| every commit | FSM/handle invariants, golden-trace replay, scripted mock scenarios | $0 offline | shipped (`evals/`, `trace_replay.py`, `tester/`) |
| every commit | **property-based FSM fuzzing** (Hypothesis `RuleBasedStateMachine`) | $0 offline | planned (see §14) |
| every commit | **tool-receipt hallucination gate** | $0 offline | shipped (`tester/`) |
| broad coverage | **autonomous adversarial persona caller** (persona+goal+stop) | cheap (tokens) | shipped (`tester/simulate.py`) |
| nightly | live persona-sim + judge, `pass^k` reliability, latency/cost gate | $ tokens | partial (`evals/sim.py`, `judge.py`, `eval-baseline`) |
| pre-release | full audio loop TTS→agent→STT + noise/accent/barge-in | $$ telephony | intentionally cut (§16) |

The prototypes off this brainstorm live in **`tester/`** (build order chosen by
an LLM council; see `tester/README.md`):

- **Tool-receipt gate** (`tester/receipt_gate.py`). A standing invariant over
  the operator-console event stream, the NABAOS *tool-receipt* pattern at its
  smallest. The terminal `outcome` event (`booked` / `cancelled` /
  `rescheduled`) is a **claim**; each `tool_call_end` with `outcome=="ok"` is a
  **receipt**. A positive claim with no matching receipt earlier in the same
  session is a `Violation`. This guards the FSM's *outcome accounting* — a
  different surface from the spoken-text regex in
  `runner._check_hallucinated_confirmation`, which it complements.
- **Recorder** (`tester/recorder.py`). `RecordingBus` + `record_call()` drive a
  scenario through the offline mock LLMs with a capturing bus attached, closing
  the gap that nothing in the harness consumed the bus as an eval oracle.
- **Offline tests** (`tester/test_receipt_gate.py`). Unit tests on hand-built
  tampered streams, plus an integration check that **every** mock scenario backs
  its outcome with a real write. Green today; goes red the moment a refactor lets
  a positive outcome fire without its tool. `make tester`; folded into
  `make verify`.
- **Autonomous call simulator** (`tester/simulate.py`, `personas.py`,
  `live_sim.py`, `invariants.py`). The literal *simulate-calls-without-dialing*
  piece: the LLM plays a goal-seeking adversarial **caller** (persona + goal +
  stop condition, generating each turn) against the **real bot**; a
  `RecordingBus` captures the call and `check_call()` audits it for the two
  hallucination invariants (unbacked outcome + spoken false confirmation). A
  curated persona library (confused elderly, wrong-then-corrected DOB, mid-call
  cancel→reschedule flip, prompt-injection name, demands an unoffered slot,
  rude-but-completes, off-topic, hallucination bait) plus `--generate N` to have
  the LLM invent fresh ones. An adversarial caller *not getting its way is not a
  failure* — only a dishonest confirmation is; exit non-zero == a real
  hallucination caught. Live (`make simulate`, needs `OPENAI_API_KEY`); the
  pytest smoke test is gated on `PROSPER_EVAL_LIVE=1` so `make verify` stays
  free. Validated: the injection persona books normally (override ignored) and
  the bait persona is refused a fake "it's cancelled" — both PASS.

## 12. Reliability

Seven layers, all in `src/prosper/llm.py`, `bot.py`, `dispatcher.py`:

1. **Tenacity retry on transient LLM failures.** `AsyncRetrying`, 3
   attempts, exponential jitter. Only retries `APIConnectionError`,
   `APITimeoutError`, `InternalServerError`, `RateLimitError`. Auth
   errors are NOT retried.
2. **Fallback model.** `PROSPER_BOT_FALLBACK_MODEL` env var. On primary
   exhaustion, one last call against the fallback (e.g. `gpt-4o-mini`
   brownout → `gpt-4o`).
3. **Startup health-check.** `_startup_health_check()` pings EHR
   `/health` before accepting clients. Soft-fail with loud warning so
   the operator sees the failure pre-call.
4. **Env fail-fast.** `load_dotenv(override=False)` (external env wins
   in containers/CI) then a required-vars check that exits with
   `SystemExit(2)` *before* the 17 s pipecat / silero / onnxruntime
   import wall when `PROSPER_BOT_ENTRYPOINT=1`.
5. **History sliding window + orphan pruning.** §7 item 4.
6. **`try/except` around `handle_user_turn`.** A stray exception is
   caught, logged, and answered with a recovery line. The WebRTC
   session survives.
7. **EHR client lifecycle via `async with`.** `async with
   dispatcher._ehr:` in `run_bot` — SIGTERM, transport crash, or a
   startup-time exception still close the client. The previous
   lifecycle (close inside the disconnect handler) leaked sockets on
   every abnormal termination.

Tests: `test_adapter_retries_on_transient_5xx_then_succeeds`,
`test_adapter_falls_back_to_secondary_model_after_retries_exhausted`,
`test_bot_dispatcher_processor.py`, `test_dispatcher_gaps.py`.

Explicitly deferred: STT/TTS multi-provider fallback (pipecat issue
[#4139](https://github.com/pipecat-ai/pipecat/issues/4139)),
pre-recorded "everything is on fire" TTS fallback.

## 13. Security

Three guards on the network + log surface:

1. **SSRF guard on `PROSPER_EHR_URL`.** `_validated_ehr_url` in
   `bot.py` rejects any scheme outside `{http, https}` and any URL
   without a hostname before building the EHR client. A compromised
   `.env` cannot repoint the bot at `169.254.169.254` (cloud metadata)
   or an internal admin endpoint. Hostname allowlisting beyond this is
   the egress firewall's job.
2. **PII redaction in log lines (HIPAA-adjacent).**
   `src/prosper/observability/redact.py` masks US-shape phone numbers,
   DOB-like strings (ISO + US + dotted), and emails in every
   `USER:` / `BOT[state]:` line. UUIDs are stashed first so the phone
   regex doesn't eat their digit-rich interior. `mask_name` is exposed
   for structured name fields. The dispatcher and transcript still see
   raw text — only `journalctl` / `loguru` sinks are masked. Not a
   substitute for a proper de-identification pipeline; good enough to
   keep ops-tier log readers from seeing caller contact details.
3. **DoS caps on request bodies.** Every Pydantic schema in
   `ehr/schemas.py` carries `Field(max_length=…)`.

Audit trail: `docs/research/2026-05-20-security-audit.md` +
`SECURITY.md`.

## 14. In-flight work

Active work the main branch does not yet reflect:

- **Mini-LLM specialty router — SHIPPED (ADR 005, 2026-05-23).** No longer
  in-flight. Landed as the `suggest_specialty` tool (BOOK_FLOW) backed by
  `llm.classify_symptoms` (a `gpt-4o-mini` JSON-mode call), variable visit
  duration {30/60/90} via an `AppointmentSlotLock` junction table, and a hard
  `medical_emergency → END` FSM edge. The hybrid-navigation `route_intent` tool
  (CHOOSE_INTENT, dispatcher-intercepted) landed alongside it. See §5, §6, ADR
  005. Scenarios `symptom_routes_to_gp`, `symptom_ambiguous_followup`,
  `direct_specialty_skips_triage` pin it; mocked offline by `MockTriageClient`.
- **Interruption design.** `docs/research/interruption_design.md` —
  research notes on how to handle the caller talking over the bot's
  TTS. Not yet wired.
- **Speculative race.** `docs/research/speculative_race.md` — research
  notes on overlapping STT partials with speculative LLM kickoff to
  reduce TTFT. Not yet wired.
- **Property-based FSM fuzzer (`tester/`).** Next prototype after the
  tool-receipt gate and the autonomous call simulator (§11). A Hypothesis
  `RuleBasedStateMachine` over the
  dispatcher generates random-but-valid caller-action programs (`@rule` =
  give phone / give DOB / pick slot / change mind) and asserts the FSM
  invariants on every dialog — tool ∈ `ALLOWED_TOOLS[state]`, no UUID
  reaches the LLM, handle round-trip, no double-book,
  confirm-only-after-successful-write. Finds whole bug *classes* via
  shrinking rather than one hand-written scenario at a time.

The eval suite pins both the plain specialty-filter behaviour
(`specialty_filter_therapist`, `specialty_unknown_falls_back`,
`specialty_no_filter_any_doctor`) and the shipped complaint→specialty router
(`symptom_routes_to_gp`, `symptom_ambiguous_followup`,
`direct_specialty_skips_triage`).

## 15. Latency (README bonus #1)

Shipped:

- **`TimingCollector` (`src/prosper/observability/timing.py`)** —
  every `_llm_turn` and `_execute_tool` wrapped in an async
  `measure(phase=, state=)` context. Emits one JSON span per call
  (`{"evt":"span","phase":"llm","state":"BOOK_FLOW","duration_ms":820}`),
  aggregates p50 / p95 / max at session end via `format_table()`.
- **TTFT.** `DispatcherProcessor` stamps `_stt_end_ts` on each final
  `TranscriptionFrame` and records `phase="ttft"` after the dispatcher
  returns. Canonical voice-agent metric in 2026; treated as a
  first-class phase in the table.
- **`cached_prompt_tokens` surfacing.** `OpenAILLMAdapter._single_call`
  pulls `usage.prompt_tokens_details.cached_tokens` and the dispatcher
  tracks both per-turn and call totals
  (`cached_prompt_tokens_total`, `prompt_tokens_total`).
- **ElevenLabs Flash v2.5** — first-audio latency ~75 ms vs ~200 ms on
  the default constructor.
- **Filler speech** in tool-firing states — `"One moment."` /
  `"Let me check."` / `"Looking that up."` rotated so the caller hears
  acknowledgement immediately rather than dead air during the LLM
  round-trip.
- **SQLite WAL** + `expire_on_commit=False` — write contention from
  ~30 ms → ~8 ms.
- **Lazy Silero VAD import** in `bot.py` — saves ~4 s of cold-import
  when `PROSPER_BOT_ENTRYPOINT=1` short-circuits.

EHR endpoint micro-bench (`make bench` → `scripts/bench.py`, 10 rounds
against `make ehr` on local NVMe):

```
endpoint           min   p50   p95   max
health             1.8   2.1   2.7   2.9
availability       8.8   9.7  10.7  11.2
by-phone (hit)     4.2   4.5   5.2   5.3
by-phone (miss)    4.1   4.6   4.9   5.7
by-name-dob        4.5   5.3   5.6   6.6
```

LLM dominates by an order of magnitude — EHR calls (httpx loopback or
ASGITransport) stay sub-15 ms; a single LLM turn is 800–2000 ms.

### 15.1 Speculative execution on call start — *the latency answer (deferred, by design)*

The highest-leverage *structural* latency idea is to overlap backend I/O with
the caller's speech: the moment a call connects (and again the moment a name is
heard), speculatively warm the paths the call is likely to take instead of doing
them sequentially on demand. Concretely:

- On `GREETING → IDENTIFY`, fire the identity lookup as soon as a name/phone is
  heard, and (for an existing patient) pre-fetch their upcoming appointments —
  so by the time the caller states an intent, "do you have anything to
  cancel/reschedule?" is already answered.
- Pre-stage the three intent branches (book / cancel / reschedule) in parallel
  rather than choosing one and then starting its I/O.
- For a new caller, prepare the registration payload speculatively (but **never**
  speculatively `create_patient` — our EHR has no name-dedupe, so a speculative
  write could duplicate a patient).

**Why it is deferred (not skipped):** on the current in-process SQLite EHR a
lookup is ~5–15 ms, so racing it saves <200 ms while adding real asyncio
task-lifecycle + cancellation complexity in the dispatcher core (drain/cancel on
`no_match` / intent-flip / call end, the MarioW333 pattern). The cost/benefit
only flips when the EHR moves out-of-process / remote (round-trips in the
100–300 ms range), where the overlap pays for the complexity. The groundwork is
already in `speculation.py` (`next_n_business_days`, fuzzy disambiguation
shipped); the async prefetch itself is held. **Scope (light warm-path vs full
3-branch race) to be decided by an LLM-council pass before building.** Tracked in
`FUTURE.md` §3.3.

## 16. Intentional cuts (deferred on purpose)

| Cut | Why |
|---|---|
| No auth / HIPAA encryption at rest | Demo scope. SQLite is dev-only. Upgrade path: Postgres + managed token service. Log-line PII redaction covers the most-likely leak surface in the meantime. |
| No multi-provider STT/TTS fallback | Pipecat has no first-class `ServiceSwitcher` (issue #4139). We ship LLM retry+fallback as the higher-value win. |
| No streaming TTS flush-after-each-clause | Biggest remaining perceived-latency win, but needs a custom Pipecat processor — out of scope for the submission window. |
| No OpenRouter / generic LLM gateway | Simplifies fallback to one env-var swap and unlocks 100+ models. Kept provider-direct so OpenAI's prompt-cache discount still applies. |
| No real audio smoke test (TTS → STT loop) | Current `evals/audio_smoke/` just asserts the dispatcher module imports; a real round-trip needs recorded WAVs + ElevenLabs credits in CI. |
| No proactive prefetch on STT partials | Brittle on partial-text changes; `ttft` phase is instrumented so we'll see the real pain before adding. |
| No `AvailabilityCache` | A 30-line dict TTL cache would shave the ~10 ms `list_availability_slots` cost — far below the LLM-dominated budget, so deferred. |
| No pre-recorded "everything is on fire" TTS fallback | Needs a checked-in WAV + regex phone capture; deferred behind the LLM retry layer that handles 99% of provider blips. |

## 17. Future work (priority order)

1. **Interruption handling** (§14) — caller talking over TTS.
2. **Speculative race** (§14) — STT partials → speculative LLM kickoff.
3. **STT/TTS multi-provider fallback** — blocked on pipecat #4139.
4. **Streaming TTS** via ElevenLabs flush-after-each-clause.
5. **OpenRouter as LLM gateway** — one env-var swap, 100+ models.
6. **Audio smoke tests** with a real TTS → STT loop.
7. **Continuous production eval** — 5–10 % sampling of live transcripts
   to the LLM judge for drift detection.
8. **Pre-recorded "everything is on fire" TTS fallback** for the
   double-failure case.
9. **`AvailabilityCache`** with 60 s TTL in `repository.py`.

Already landed (was on this list): mock-eval offline mode, parallel
eval runner, atomic reschedule, specialty filter, next-day forward
scan, operator console event stream, **mini-LLM specialty router +
hybrid `route_intent` navigation (ADR 005)**.

## 18. File map

- `src/prosper/ehr/` — FastAPI app, SQLAlchemy models, repository,
  schemas, db engine (auto-migration for `Provider.specialty`).
- `src/prosper/dispatcher.py` — FSM runtime, transcript, tool-whitelist
  enforcement, handle redaction, memory validation, history pruning,
  console bus wiring.
- `src/prosper/flows.py` — state graph topology + per-state tool
  whitelist (plain data).
- `src/prosper/tools.py` — 8 tool handlers + `TOOL_SCHEMAS` (OpenAI
  function-calling shapes) + `HANDLERS` map.
- `src/prosper/prompts.py` — `CLINIC_PERSONA`, per-state
  `TASK_MESSAGES`, `STATE_FILLERS`, `FALLBACK_LINES`. All caller-
  audible strings.
- `src/prosper/llm.py` — `OpenAILLMAdapter` (implements
  `LLMClientProtocol`); retry, fallback model, usage surfacing.
- `src/prosper/ehr_client.py` — `EHRClient` (httpx) with X-Request-Id
  threading + SSRF-validated base URL.
- `src/prosper/result.py` — `Result[Ok, Err]` discriminated union.
- `src/prosper/observability/timing.py` — `TimingCollector` + JSON
  span logs.
- `src/prosper/observability/redact.py` — `redact_pii`, `mask_name`,
  `mask_phone`.
- `src/prosper/console/` — `events.py` (8 event types), `bus.py`
  (bounded async queues + overflow drop), `sse.py`, `server.py`,
  `audit.py`, `_utils.py`.
- `src/prosper/bot.py` — Pipecat pipeline wiring, `DispatcherProcessor`,
  SSRF guard, env fail-fast, lazy VAD import.
- `evals/` — `Scenario` / `StateExpectation` types, `PersonaSimulator`,
  judge, runner (parallel, baseline), CLI.
- `evals/mock_llm.py` — deterministic mock LLM + persona scripts.
- `scripts/bench.py` — EHR endpoint micro-bench.
- `scripts/status.py` — one-shot repo health snapshot (`make status`).
- `scripts/seed.py` — seed `data/ehr.db` (auto-runs on empty DB).
- `docs/adr/001..004` — ADRs: hybrid FSM, separate EHR process, paired
  eval, operator console.
- `docs/architecture.md` — ASCII process + FSM diagrams.
- `docs/bench-results.md` — pinned bench snapshots.
- `docs/glossary.md` — terminology cheat-sheet.
- `docs/research/` — codebase-audit, security-audit, prod-readiness,
  reliability, eval-depth, latency-advanced, perf-wave2, interruption
  design, speculative race.
- `docs/superpowers/specs/2026-05-19-prosper-challenge-design.md` —
  full deliberation trail (LLM council verdict per decision).
- `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`, `ERRORS.md`,
  `FUTURE.md`, `.editorconfig`, `.gitattributes` — repo hygiene +
  contributor surface.
