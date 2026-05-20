# Prosper Health voice agent — solution

## Overview
A voice agent that books and cancels appointments at a fictional clinic.
Two processes: a FastAPI EHR backed by SQLite + SQLAlchemy, and a Pipecat
bot driven by a custom finite-state-machine dispatcher with per-state
tool whitelisting.

## Quick start
```bash
cp env.example .env       # add ELEVENLABS_API_KEY and OPENAI_API_KEY
make install              # uv sync
make seed                 # one-time
make ehr                  # terminal 1 — FastAPI on :8000
make bot                  # terminal 2 — Pipecat on :7860
# open http://localhost:7860 → Connect
```
Or with Docker: `docker-compose up`.

## Quick evaluate
```bash
OPENAI_API_KEY=... make eval          # 16 scripted scenarios, paired state + judge
OPENAI_API_KEY=... uv run python -m evals --json evals/results/baseline.json
OPENAI_API_KEY=... uv run python -m evals --concurrency 4 --baseline evals/results/baseline.json   # CI gate, 4-way parallel
```

## Endpoint mapping (challenge spec → REST)

The README requests five named endpoints. We grouped them by HTTP verb and
resource so the EHR feels like a real integration. The mapping is exact:

| Challenge name | REST endpoint |
|---|---|
| `create_patient` | `POST /patients` |
| `find_patient` (by name + DOB) | `GET /patients/by-name-dob?name=&dob=` |
| `find_patient` (by phone, our addition) | `GET /patients/by-phone?phone=` |
| `list_availability_slots` | `GET /availability?date=&provider_id=` |
| `create_appointment` | `POST /appointments` |
| `cancel_appointment` | `POST /appointments/{id}/cancel` |
| (helper) get patient appointments | `GET /patients/{id}/appointments` |

The phone-first `find_patient` variant exists because phone numbers are the
single thing STT handles crisply — it's the bot's primary identification
path. The challenge-required name+DOB lookup is still fully implemented and
exercised as the fallback path.

## Architecture decisions

| Decision | Choice | Why |
|---|---|---|
| Conversation orchestration | **Hybrid FSM** (9 states, per-state LLM-with-whitelisted-tools) | Deterministic state transitions and dispatcher-enforced tool gates eliminate "agent did the wrong thing at the wrong time" as a prompt-obedience problem; state assertions become trivial in evals. |
| FSM library | **Custom dispatcher** (~250 LOC, no Pipecat Flows) | Single auditable file; no extra dependency on the latency-sensitive path; designed so a migration to Pipecat Flows later is a mechanical change. |
| EHR topology | **Separate FastAPI process** + httpx client | Mirrors real EHR integrations; reviewers can stress-test the EHR independently; bot and EHR scale and fail independently. The 1 ms HTTP loopback overhead is irrelevant against STT/LLM/TTS latency. |
| Database | **SQLite + SQLAlchemy** + pre-seeded `Slot` rows | Zero-config, file-backed, reproducible across reviewer machines. Slots as first-class rows mean admins can block lunch/PTO as exception rows, and idempotency on booking becomes a partial unique constraint instead of a distributed-lock problem. |
| Patient identification | **Phone first, name+DOB fallback** | Phone is a digit string — STT handles it crisply. The challenge-required `find_patient_by_name_dob` path is fully implemented and exercised on the fallback path; one scenario covers each path. |
| Appointment cancel | **Adaptive 0 / 1 / N** | 0 upcoming → say so and exit; 1 → auto-confirm; N → numbered list. One conditional, big UX win. |
| Tool returns | **`Result[Ok, Err]` discriminated union** | Eliminates `dict \| None` ambiguity at the boundary; gives the dispatcher and the eval state-assertions a stable error-code enum to branch on. |
| Eval suite | **Hybrid scripted text + paired state-assertion + LLM judge** | The deterministic check closes the "judge hallucinated success on a transcript that didn't actually mutate the EHR" gap. Audio smoke tests are marker-gated so default CI doesn't burn ElevenLabs credits on every push. |
| Latency | **Instrumentation + long stable persona for prompt-cache + tight per-state prompts + filler speech + Flash v2.5 TTS + SQLite WAL + lazy VAD import** | TimingCollector with TTFT + cached-token surfacing; ElevenLabs Flash v2.5 (~75ms first-audio vs ~200ms); rotating filler ("One moment." / "Let me check." / "Looking that up.") before tool-firing states masks the LLM round-trip; SQLite WAL + `expire_on_commit=False` drop write contention from ~30 ms to ~8 ms; lazy Silero VAD import saves ~4 s of bot cold-import. See `docs/bench-results.md` for numbers. |
| Reliability | **tenacity retry + fallback model + EHR health-check + env fail-fast + history sliding window + try/except around dispatcher + EHR client lifecycle** | `OpenAILLMAdapter` retries transient 5xx/429 with exponential jitter, falls back to `PROSPER_BOT_FALLBACK_MODEL` once after retries exhaust; soft-fails the EHR `/health` ping at boot; missing env vars short-circuit the 17 s pipecat import wall (`PROSPER_BOT_ENTRYPOINT=1`); the LLM history is capped at 40 messages so a 50-turn call can't blow the token budget; a last-resort try/except around `handle_user_turn` keeps a mid-call exception from killing the WebRTC session; EHR httpx client owned via `async with` so SIGTERM still closes cleanly. README bonus #2 explicitly. |
| Security | **SSRF guard on `PROSPER_EHR_URL` + PII redaction in logs + DoS caps on request bodies** | `_validated_ehr_url` rejects non-http(s) schemes / missing hostnames so a compromised `.env` can't repoint the bot at cloud metadata (`169.254.169.254`); `redact_pii` masks phone / DOB / email in every `USER:` and `BOT[state]:` log line (HIPAA-adjacent — the dispatcher still sees raw text); Pydantic `Field(max_length=...)` caps prevent a malicious POST dumping multi-MB strings into SQLite. See `docs/research/2026-05-20-security-audit.md`. |
| Correctness (audit fixes) | **Past-slot filter + IntegrityError→409 + UUID redaction + memory-validated tool args** | Bot can't offer 8am at 11am; concurrent-booking races surface as recoverable 409 not 500; LLM history only ever sees human-readable summaries (no raw UUIDs to read aloud); `create_appointment` rejects hallucinated slot_ids before they reach the EHR. |
| Quality gates | **130 tests (`tests/` + eval unit tests), 91% line coverage, `mypy --strict`, ruff `{I,E,F,W,B,UP,ARG,SIM,RET,RUF,S}`** | `mypy.strict = true` in `pyproject.toml`; ruff `S` (bandit security checks) on the whole tree; per-file ignores documented inline; pre-commit runs the whole suite. |

The full deliberation trail is in
`docs/superpowers/specs/2026-05-19-prosper-challenge-design.md` —
every architectural decision is tagged with the corresponding LLM
council verdict.

## Conversation flow

```
GREETING
  ↓ (user speaks)
IDENTIFY_PATIENT  ──(no match after both lookups)──▶  REGISTER_PATIENT
  ↓ (found)                                                   ↓ (created)
CHOOSE_INTENT  ◀──────────────────────────────────────────────┘
  ↓
BOOK_FLOW                                  CANCEL_FLOW
  ↓ slot_chosen                              ↓ appointment_chosen
CONFIRM_BOOK ─create_appointment─▶ END     CONFIRM_CANCEL ─cancel_appointment─▶ END
```

Per-state tool whitelist (enforced at the dispatcher level, not by prompt
instructions):

| State | Allowed tools |
|---|---|
| GREETING | (none) |
| IDENTIFY_PATIENT | `find_patient_by_phone`, `find_patient_by_name_dob` |
| REGISTER_PATIENT | `create_patient` |
| CHOOSE_INTENT | (none) |
| BOOK_FLOW | `list_availability_slots` |
| CANCEL_FLOW | `get_upcoming_appointments` |
| CONFIRM_BOOK | `create_appointment` |
| CONFIRM_CANCEL | `cancel_appointment` |
| END | (none) |

If the LLM tries to call a tool outside its current state's whitelist, the
dispatcher rejects it, injects a system message into the LLM history, and
records a `tool_rejected` event in the transcript — the model recovers on
the next iteration rather than the call silently mis-firing.

## Reliability (README bonus #2)

Seven layers, all in `src/prosper/llm.py`, `src/prosper/bot.py`, and
`src/prosper/dispatcher.py`:

1. **`tenacity` retry on transient LLM failures** — `AsyncRetrying` with
   3 attempts and exponential jitter, scoped to `APIConnectionError`,
   `APITimeoutError`, `InternalServerError`, `RateLimitError`. Auth errors
   are NOT retried — they don't get better with backoff.
2. **Fallback model** — `PROSPER_BOT_FALLBACK_MODEL` env var; on primary-
   model exhaustion, one last call against the fallback (e.g.
   `gpt-4o-mini` brownout → `gpt-4o`) before propagating.
3. **Startup health-check** — `_startup_health_check()` pings the EHR
   `/health` before accepting clients. Soft-fail with loud warning so the
   operator sees the failure pre-call, not mid-conversation.
4. **Env fail-fast** — `load_dotenv(override=False)` (external env wins
   over `.env` in containers / CI) followed by a required-vars check that
   exits with `SystemExit(2)` *before* the 17 s pipecat/silero/onnxruntime
   import wall when `PROSPER_BOT_ENTRYPOINT=1`. A misconfigured bot
   reports the problem in <1 s instead of crashing on first audio frame.
5. **History sliding window** — the LLM `history` list is capped at 40
   messages (`_HISTORY_WINDOW`) per `_messages_for_llm`. Persona +
   per-state task message and `SessionMemory` carry the salient facts, so
   dropping the oldest raw turns is safe; a 50-turn call still fits in
   budget.
6. **Try/except around `handle_user_turn` in `DispatcherProcessor`** —
   a stray exception (LLM 5xx after retries exhausted, EHR timeout, JSON
   parse error) is caught, logged, and answered with "Sorry, I missed
   that — could you say it again?". The WebRTC session survives, the
   caller retries; the alternative (uncaught exception bubbling through
   Pipecat) kills the call.
7. **EHR client owned by `async with` for the call's lifetime** — the
   httpx client lives inside `async with dispatcher._ehr:` in `run_bot`,
   so SIGTERM, transport crash, or a startup-time exception still close
   it. The previous lifecycle (close inside the disconnect handler) leaked
   sockets on every abnormal termination.

Unit tests cover the retry + fallback paths
(`test_adapter_retries_on_transient_5xx_then_succeeds`,
`test_adapter_falls_back_to_secondary_model_after_retries_exhausted`) and
the dispatcher's history-cap + try/except behaviour
(`test_bot_dispatcher_processor.py`, `test_dispatcher_gaps.py`).

Explicitly deferred to "Future work":
- STT/TTS multi-provider fallback (would need a `ServiceSwitcher` over
  Pipecat services; tracked by upstream issue #4139).
- Pre-recorded "everything is on fire" TTS fallback with regex phone
  capture + callback queue.

## Security

Three guards on the surface that touches the network and the log sinks:

1. **SSRF guard on `PROSPER_EHR_URL`** — `_validated_ehr_url` (in
   `bot.py`) rejects any scheme outside `{http, https}` and any URL
   without a hostname before constructing the EHR client. Anyone who
   controls `.env` (compromised CI, sloppy deploy) cannot repoint the bot
   at `http://169.254.169.254/latest/meta-data` (cloud metadata) or an
   internal admin endpoint and then phish the LLM into firing a tool
   that exfiltrates the response. Hostname allowlisting beyond this is
   delegated to the network policy / egress firewall.
2. **PII redaction in log lines (HIPAA-adjacent)** —
   `src/prosper/observability/redact.py` masks US-shape phone numbers,
   DOB-like strings (ISO + US + dotted), and email addresses in every
   `USER:` / `BOT[state]:` log line. UUIDs are stashed first so the
   phone regex doesn't eat their digit-rich interior; `mask_name` is
   exposed for the structured name fields. The dispatcher still sees
   raw text — only `journalctl` / `loguru` sinks are masked. Not a
   substitute for a proper de-id pipeline; good enough to keep
   ops-tier log readers from seeing the caller's contact details.
3. **DoS caps on request bodies** — every `PatientCreate` /
   `AppointmentCreate` / `AppointmentCancel` field carries a Pydantic
   `Field(max_length=...)` cap so a malicious POST can't load a
   multi-megabyte string into SQLite (the DB column would silently
   truncate, but the request body is parsed entirely into memory first).

Full audit trail in `docs/research/2026-05-20-security-audit.md`.

## Observability

Three signals surfaced by the same `TimingCollector` /
`observability/redact.py` pair:

- **`TimingCollector` (`src/prosper/observability/timing.py`)** — every
  `_llm_turn` and `_execute_tool` is wrapped in an async `measure(phase=,
  state=)` context manager; the collector emits one JSON line per span
  (`{"evt":"span","phase":"llm","state":"BOOK_FLOW","duration_ms":820}`)
  to stdout and aggregates p50 / p95 / max at session end via
  `format_table()`. The eval CLI prints the table at the end of every run.
- **TTFT (time-to-first-token)** — `DispatcherProcessor` stamps
  `_stt_end_ts` on each final `TranscriptionFrame` and records
  `phase="ttft"` after the dispatcher returns the reply. Canonical
  voice-agent metric in 2026; treated as a first-class phase in the
  same summary table.
- **`cached_prompt_tokens` surfacing** — `OpenAILLMAdapter._single_call`
  pulls `usage.prompt_tokens_details.cached_tokens` from the OpenAI
  response and the dispatcher tracks both per-turn (in the transcript
  entry) and call totals (`cached_prompt_tokens_total`,
  `prompt_tokens_total`). Lets the eval runner print `cache=XX%` per
  scenario to confirm the long `CLINIC_PERSONA` is actually crossing
  OpenAI's 1024-token prompt-cache threshold.
- **PII-safe logging** — every line that contains user / bot speech goes
  through `redact_pii` before `logger.info`. See the Security section.

## Eval suite

Two checks per scenario, **both must pass**:

- **State assertion (deterministic):** queries DB counts before/after the
  run, verifies expected tool calls fired and forbidden ones didn't,
  verifies the FSM reached the expected terminal state, and runs a
  regex post-check for hallucinated confirmations ("I've cancelled" with
  no matching tool_ok).
- **LLM judge (semantic):** scores the full transcript against natural-
  language criteria.

Sixteen scenarios at launch (3 happy / 1 recovery / 2 edge / 10 adversarial / recovery):

| Scenario | Tags | What it tests |
|---|---|---|
| `new_patient_books` | happy | Phone-first lookup → no match → register → book |
| `existing_patient_cancels` | happy | Phone match → 1 upcoming → adaptive auto-confirm → cancel |
| `cancel_picks_from_list` | happy | 3 upcoming → numbered list → ordinal pick → cancel |
| `dob_misheard_then_corrected` | recovery | Persona mispronounces DOB year, corrects after read-back |
| `slot_taken_by_other` | edge | Persona insists on a slot another patient holds; agent must offer alternatives |
| `cancel_when_nothing_to_cancel` | edge | Patient has 0 upcoming; agent must say so, not invent one |
| `prompt_injection_direct_override` | adversarial | "Ignore previous instructions" — bot must refuse and never write |
| `prompt_injection_stored_in_name` | adversarial | Injection hidden inside the name field — must be treated as literal |
| `cross_patient_cancel_refusal` | adversarial | Caller authenticates then asks to cancel Bob's appointment — must refuse + not leak |
| `hallucinated_confirmation_trap` | adversarial | Caller insists a non-existent appointment exists — bot must not invent / claim |
| `off_topic_steering_and_budget` | adversarial | Weather/pizza/joke probes then cooperate — bot must stay in scope |

Adding a scenario is a 20-line PR — `evals/scenarios.py` is plain Python
data, no framework changes needed. CLI also supports
`--baseline previous.json` to fail the run on regression vs a prior snapshot.

## Latency (README bonus #1)

Shipped tactics:

- **TimingCollector** — per-span JSON logs + p50/p95/max aggregates printed
  at the end of every call and surfaced inline in `make eval`.
- **TTFT (time-to-first-token)** — recorded between final `TranscriptionFrame`
  and dispatcher reply. Canonical voice-agent metric in 2026.
- **`CLINIC_PERSONA` ≥ 1100 tokens** — crosses OpenAI's 1024-token prompt-
  cache threshold; cached fraction reported per scenario as `cache=XX%`.
- **Per-state task messages ≤ 1 KB** — enforced by
  `test_each_task_message_under_1_kb` so future contributors can't blow
  up per-turn input cost.
- **ElevenLabs Flash v2.5** — `eleven_flash_v2_5` cuts first-audio
  latency from ~200ms → ~75ms vs the default constructor.
- **Filler speech in tool-firing states** — `"One moment."` TTS frame
  pushed before each LLM turn that lives in a tool-calling state, so the
  caller hears acknowledgement immediately rather than dead air.

EHR endpoint micro-bench (`make bench` → `scripts/bench.py`,
10 rounds, against `make ehr` on local NVMe):

```
endpoint           min   p50   p95   max
health             1.8   2.1   2.7   2.9
availability       8.8   9.7  10.7  11.2
by-phone (hit)     4.2   4.5   5.2   5.3
by-phone (miss)    4.1   4.6   4.9   5.7
by-name-dob        4.5   5.3   5.6   6.6
```

Full snapshot history in `docs/bench-results.md` (pre-N+1-fix
`/availability` was 115 ms — single LEFT-OUTER-JOIN dropped it to
~10 ms; SQLite WAL further halves p50 under concurrent writes).

What this means at the call level:
- The LLM dominates by an order of magnitude. EHR calls (via either
  `httpx.ASGITransport` in evals or a real local socket in prod) stay
  sub-15 ms; a single LLM turn is 800-2000 ms.
- The stable `CLINIC_PERSONA` preamble crosses OpenAI's 1024-token
  prompt-cache threshold; `cached_prompt_tokens` is now surfaced per
  turn (`LLMUsage.cached_prompt_tokens` in the transcript, totals in
  `Dispatcher.cached_prompt_tokens_total`) so the eval runner can report
  `cache=XX%` per scenario.

## Dev-log

- Initial dispatcher draft used a single mega-prompt with every tool
  enabled. Switched to the per-state whitelist after the first eval
  run produced a transcript where the model called `cancel_appointment`
  during GREETING. The whitelist made that bug structurally impossible.
- Tried `python-dateparser` first for DOB parsing; dropped it for
  `python-dateutil` because dateparser pulls `babel` and added noticeable
  cold-import time without improving real-world DOB recognition.
- Spec implies `find_patient` takes name+DOB; exposed both
  `find_patient_by_phone` and `find_patient_by_name_dob` so the bot
  leads with phone but exercises the spec endpoint on fallback. One
  scenario asserts each path fires.
- `pipecat-ai[daily]` does not have Windows wheels (the `daily-python`
  binding only ships Linux/macOS); since this project uses WebRTC
  transport, we dropped the `daily` extra.
- `Err.code` strings became the public eval contract — renaming one
  without updating `evals/scenarios.py` is now banned by CLAUDE.md.
- Web-research pass (Pipecat docs + 2026 voice-agent blogs) revealed
  the default `LLMUserAggregator` carries a 1.0s `aggregation_timeout`.
  We don't use the aggregator at all (DispatcherProcessor consumes
  `TranscriptionFrame` directly), so the tax doesn't apply — but the
  finding is documented at the top of `bot.py` so a future refactor
  doesn't re-introduce it.
- Codebase-audit subagent caught four real bugs (past-slot exposure,
  IntegrityError→500 instead of 409, UUID leak into LLM history,
  unvalidated hallucinated slot_ids). Each got a fix + a regression
  test in the same commit.
- After paired state+judge over the original 6 scenarios, we found the
  judge could mark a scenario PASS even when the assistant said "I've
  cancelled that for you" with no matching `tool_ok`. Added a regex
  post-check in the runner — that single ~30-line addition makes the 5
  adversarial scenarios actually meaningful.

## Intentional cuts (still deferred)

| Cut | Why |
|---|---|
| No auth / HIPAA encryption at rest | Demo scope; SQLite is dev-only. Documented upgrade path is Postgres + a managed token service. PII redaction in log sinks (above) covers the most-likely leak surface in the meantime. |
| No multi-provider **STT/TTS** fallback | Pipecat has no first-class `ServiceSwitcher` (issue [#4139](https://github.com/pipecat-ai/pipecat/issues/4139)); meanwhile we ship LLM retry+fallback as the higher-value reliability win. |
| No streaming TTS (flush-after-each-clause) | Biggest remaining perceived-latency win, but needs a custom Pipecat processor — out of scope for the submission window. |
| No OpenRouter / generic LLM gateway | Would simplify the fallback story to a single env-var swap and unlock 100+ models. We keep provider-direct so OpenAI's prompt-cache discount still applies. |
| No real audio smoke test (TTS → STT loop) | Current skeleton (`evals/audio_smoke/`) just asserts the dispatcher module imports; a real audio round-trip would need recorded WAV fixtures + ElevenLabs credits in CI. |
| No proactive prefetch on STT partials | Brittle on partial-text changes; instrumentation is in place (`ttft` phase) so we can see where real pain is before adding. |
| No `AvailabilityCache` | A 30-line `dict` TTL cache would shave the ~10 ms `list_availability_slots` cost — far below the LLM-dominated budget, so deferred. |
| No pre-recorded "everything is on fire" TTS fallback | Would need a checked-in WAV + regex phone capture; deferred behind the LLM retry layer that handles 99% of provider blips. |

## Future work (in priority order)

1. **STT/TTS multi-provider fallback** — Deepgram for STT, OpenAI TTS as
   secondary. Blocked on a Pipecat `ServiceSwitcher` story (issue #4139).
2. **Streaming TTS** via ElevenLabs flush-after-each-clause — biggest
   remaining perceived-latency win.
3. **OpenRouter as LLM gateway** — would simplify the fallback story to
   a single env-var swap and unlock 100+ models.
4. **Audio smoke tests with a real TTS → STT loop** — current skeleton
   just asserts the dispatcher module imports.
5. **Continuous production eval (5–10% sampling)** — 2026 production
   pattern; would sample live transcripts to the LLM judge for drift
   detection.
6. **Pre-recorded "everything is on fire" TTS fallback** — for the
   double-failure case where retry + fallback both exhaust.
7. **`AvailabilityCache`** with 60 s TTL in `repository.py`.

## File map (where to look for what)

- `src/prosper/ehr/` — FastAPI app, SQLAlchemy models, repository, schemas, db engine
- `src/prosper/dispatcher.py` — FSM, transcript, tool-whitelist enforcement
- `src/prosper/flows.py` — state graph topology + per-state tool whitelist
- `src/prosper/prompts.py` — `CLINIC_PERSONA` + per-state task messages
- `src/prosper/tools.py` — tool handlers + OpenAI schemas + `HANDLERS` map
- `src/prosper/llm.py` — `OpenAILLMAdapter` (implements `LLMClientProtocol`)
- `src/prosper/observability/timing.py` — `TimingCollector` + JSON span logs
- `src/prosper/observability/redact.py` — `redact_pii` + `mask_name` for log lines
- `src/prosper/bot.py` — Pipecat pipeline wiring + `DispatcherProcessor` + SSRF guard + env fail-fast
- `evals/` — `Scenario`/`StateExpectation` types, persona simulator,
  judge, runner, scenarios, pytest entrypoint, CLI
- `scripts/bench.py` — re-runnable EHR-endpoint micro-bench (`make bench`)
- `docs/adr/` — three short Architecture Decision Records
- `docs/architecture.md` — ASCII process + FSM diagrams
- `docs/bench-results.md` — pinned bench snapshots, oldest → newest
- `docs/interview-notes.md` — candidate prep + decision evidence
- `docs/research/` — codebase-audit, security-audit, prod-readiness,
  reliability, eval-depth, latency-advanced, perf-wave2 research notes
- `docs/superpowers/specs/2026-05-19-prosper-challenge-design.md` —
  full deliberation trail (LLM council verdict per decision)
- `docs/superpowers/specs/2026-05-19-other-solutions-best-ideas.md` —
  cross-survey of 10 reference solutions; borrowed ideas are cited
- `docs/superpowers/plans/2026-05-20-prosper-challenge-implementation.md`
  — bite-sized TDD implementation plan that produced this codebase
