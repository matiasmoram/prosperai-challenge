# Glossary

Repo-specific vocabulary. One sentence each, with a pointer to the source of
truth in code.

- **TTFT** — Time-to-first-token; the latency from user end-of-speech to the
  first LLM token streaming back, instrumented per turn
  (`src/prosper/observability/timing.py`, surfaced in transcript logs).
- **Dispatcher** — The async orchestrator that owns conversational memory,
  decides which tools the LLM is allowed to call this turn, executes them,
  redacts results, and drives FSM transitions (`src/prosper/dispatcher.py`).
- **FSM** — Finite-state machine governing the call flow; not a heuristic,
  not a graph of prompts — a typed enum + transition table
  (`src/prosper/flows.py`).
- **State** — One node in the FSM (`GREETING`, `IDENTIFY_PATIENT`, …); each
  state has a system prompt and a tool whitelist
  (`src/prosper/flows.py::State`).
- **Tool whitelist** — Per-state set of tool names the LLM may invoke;
  out-of-whitelist tool calls are dropped before reaching the handler
  (`src/prosper/flows.py::ALLOWED_TOOLS`).
- **Result[Ok, Err]** — Sum type every tool handler returns; `Err.code` is
  public contract that scenarios assert on, never silently rename
  (`src/prosper/result.py`).
- **CLINIC_PERSONA** — The ~1100-token shared preamble cached by the
  provider once and reused across every turn, keeping per-state prompts
  small (`src/prosper/prompts.py::CLINIC_PERSONA`).
- **Prompt cache** — OpenAI's automatic reuse of the static prefix of a
  request; we exploit it by putting CLINIC_PERSONA first and per-state task
  messages last, then surface `cached_prompt_tokens` in timings
  (`src/prosper/llm.py`, `src/prosper/observability/timing.py`).
- **Paired state-assertion** — Eval pattern requiring both a deterministic
  final-state check AND an LLM-judge pass for a scenario to be marked
  `overall_pass`; defeats judge hallucination
  (`docs/adr/003-paired-state-assertion-plus-judge.md`,
  `evals/runner.py::ScenarioResult`).
- **HeadlessFlow runner** — Test-mode pipeline that drives the dispatcher
  with scripted user turns instead of real STT/TTS, used by the eval suite
  (`evals/runner.py::HeadlessFlow`).
- **Eval baseline** — Pinned JSON snapshot of scenario pass/fail used to
  diff regressions across waves
  (`evals/results/baseline.json`, `make eval-baseline`).
- **hallucinated_confirmation** — Adversarial regex check catching the LLM
  saying "you're booked" before the create_appointment tool actually fired
  (`evals/runner.py`, scenario in `evals/scenarios.py`).
- **SSRF guard** — Startup-time URL validation on `PROSPER_EHR_URL` that
  rejects non-http(s) schemes and missing hostnames, blocking redirection
  to cloud metadata endpoints (`src/prosper/bot.py::_validated_ehr_url`).
- **Operator Console** — Read-only web view on `:7861` that renders the
  live dispatcher state, tool calls, identified patient (PII redacted),
  slots offered, transcript, and outcome of a call (`src/prosper/console/`,
  `docs/adr/004-operator-console-event-stream.md`).
- **ConsoleEvent** — Frozen dataclass carrying one operator-console
  telemetry record (`type`, `ts`, `session_id`, `payload`). The 8 valid
  types form a closed `Literal` (`src/prosper/console/events.py`).
- **ConsoleBus** — In-memory async pub/sub bus with bounded per-subscriber
  queues. Publication is non-blocking; overflow drops the oldest event
  for the slow subscriber (`src/prosper/console/bus.py`).
- **Audit JSONL** — Append-only `.jsonl` file (one per session) under
  `data/audit/`, written by `AuditJSONLWriter` as it drains the bus.
  Source of truth for the `/console/replay/{id}` endpoint
  (`src/prosper/console/audit.py`).
- **triage / suggest_specialty** — `BOOK_FLOW`-only tool that maps a caller's
  free-form symptom description to a best-fit specialty + visit duration via a
  `gpt-4o-mini` JSON-mode call (`llm.classify_symptoms`,
  `tools.suggest_specialty_handler`, ADR 005). Skipped when the caller names a
  specialty directly.
- **SpecialtyClassification** — Frozen dataclass returned by `classify_symptoms`:
  `specialty`, `duration_minutes`, `confidence`, `follow_up?`, `red_flag`
  (`src/prosper/llm.py`).
- **AppointmentSlotLock** — One-row-per-slot table (PK on `slot_id`) that enforces
  no-double-booking across multi-slot (60/90-min) appointments. A 60-min visit
  locks two consecutive slots in one transaction (`src/prosper/ehr/models.py`,
  ADR 005).
- **duration_minutes** — Visit length in `{30, 60, 90}` carried on `Appointment`
  and threaded through `list_availability_slots` / `create_appointment`. >30-min
  bookings require N consecutive free slots under the same provider.
- **NoConsecutiveSlotsError / no_consecutive_slots** — Raised/returned when a
  60- or 90-min booking can't find enough adjacent free slots; surfaces as 409
  the LLM can recover from by offering another time.
- **medical_emergency** — `Err.code` from `suggest_specialty` when the triage
  mini-LLM flags a red-flag symptom (chest pain, suicidal ideation, …). The
  agent redirects to emergency services and never books.
- **MockTriageClient** — Deterministic keyword-routing stub for the triage
  mini-LLM, installed via `prosper.llm._TRIAGE_CLIENT_OVERRIDE` so `make
  mock-eval` stays hermetic (`evals/mock_llm.py`).
