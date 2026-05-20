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
