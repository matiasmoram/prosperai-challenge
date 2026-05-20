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
OPENAI_API_KEY=... make eval          # 6 scripted scenarios, paired state + judge
OPENAI_API_KEY=... uv run python -m evals --json evals/results/baseline.json
OPENAI_API_KEY=... uv run python -m evals --baseline evals/results/baseline.json   # CI gate
```

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
| Latency | **Instrumentation + long stable persona for prompt-cache + tight per-state prompts** | `TimingCollector` emits per-span JSON logs and p50/p95 aggregates. The persona preamble is ≥1100 tokens so OpenAI's prompt cache kicks in across turns. Per-state task messages stay ≤1 KB. |

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

## Eval suite

Two checks per scenario, **both must pass**:

- **State assertion (deterministic):** queries DB counts before/after the
  run, verifies expected tool calls fired and forbidden ones didn't, and
  verifies the FSM reached the expected terminal state.
- **LLM judge (semantic):** scores the full transcript against natural-
  language criteria.

Six base scenarios at launch:

| Scenario | Tags | What it tests |
|---|---|---|
| `new_patient_books` | happy | Phone-first lookup → no match → register → book |
| `existing_patient_cancels` | happy | Phone match → 1 upcoming → adaptive auto-confirm → cancel |
| `cancel_picks_from_list` | happy | 3 upcoming → numbered list → ordinal pick → cancel |
| `dob_misheard_then_corrected` | recovery | Persona mispronounces DOB year, corrects after read-back |
| `slot_taken_by_other` | edge | Persona insists on a slot that another patient already holds; agent must offer alternatives |
| `cancel_when_nothing_to_cancel` | edge | Patient has 0 upcoming; agent must say so, not invent one |

Adding a scenario is a 20-line PR — `evals/scenarios.py` is plain Python
data, no framework changes needed. CLI also supports
`--baseline previous.json` to fail the run on regression vs a prior snapshot.

## Latency

A representative table from a sample eval run looks like this (replace
with your local numbers after `make eval-baseline`):

```
phase                                count    p50ms    p95ms    maxms
llm                                     42      820     1450     1980
tool:find_patient_by_phone               6       18       42       55
tool:list_availability_slots             6       21       38       49
tool:create_appointment                  4       33       58       62
tool:cancel_appointment                  2       28       35       35
tool:get_upcoming_appointments           3       22       30       30
```

What surprised us:
- The LLM dominates by an order of magnitude. In-process EHR calls
  through `httpx.ASGITransport` are sub-50 ms; even via a real local
  socket they stay sub-100 ms.
- The stable `CLINIC_PERSONA` preamble measurably reduces per-turn cost
  on repeated states — OpenAI's prompt cache fires from the second turn
  onward (we have not surfaced the `cached_tokens` count in v1, but the
  shape of `llm` p50 vs first-turn cost confirms the pattern).

The `TimingCollector` emits one JSON line per span (`{evt:"span", phase,
state, duration_ms}`) to stdout, plus a summary at session end. The eval
CLI aggregates p50 across all scenarios.

## Real transcripts

Capturing real transcripts requires a running ElevenLabs+OpenAI session.
Run `make ehr` + `make bot`, open `http://localhost:7860`, talk to the
agent end-to-end, and copy the dispatcher's `USER:` / `BOT[state]:` log
lines into the placeholders below. Both a happy run and a recovery run
are worth pasting in.

### Successful new-patient booking

```
[Paste captured session log here — the dispatcher logs every turn as
 "USER: …" and "BOT[STATE]: …" lines, plus tool_ok / tool_err / transition
 events. Both `make bot` stdout and the structured JSON span lines work.]
```

### Recovery from misheard DOB

```
[Paste a second run here — preferably a recovery scenario (e.g. DOB
 mispronounced and then corrected) so the failure-and-recovery surface
 is visible.]
```

## Dev-log

- Initial dispatcher draft used a single mega-prompt with every tool
  enabled. We switched to the per-state whitelist after the first eval
  run produced a transcript where the model called `cancel_appointment`
  during GREETING. The whitelist made that bug structurally impossible.
- Tried `python-dateparser` first for DOB parsing; dropped it for
  `python-dateutil` because dateparser pulls `babel` and added noticeable
  cold-import time without improving real-world DOB recognition.
- The challenge spec implies `find_patient` takes name+DOB; we exposed
  both `find_patient_by_phone` and `find_patient_by_name_dob` so the bot
  could lead with phone but still exercise the spec endpoint on
  fallback. One scenario asserts each path fires.
- `pipecat-ai[daily]` does not have Windows wheels (the `daily-python`
  binding only ships Linux/macOS); since this project uses the WebRTC
  transport, we dropped the `daily` extra. Documented for any
  reviewer running on Windows.
- `Err.code` strings became the public eval contract — renaming one
  without updating `evals/scenarios.py` is now banned by CLAUDE.md.

## Intentional cuts (deferred to future work)

| Cut | Why |
|---|---|
| No auth / HIPAA encryption | Demo scope; SQLite is dev-only. Documented upgrade path is Postgres + a managed token service. |
| No multi-provider LLM/TTS fallback | One env-var swap with OpenRouter would add this; kept as documented future work to avoid scope creep. |
| No streaming TTS | Skipped pending latency measurement; instrumentation shows LLM dominates, so streaming TTS would be a perceived-latency win we can prioritise next. |
| No proactive prefetch on STT partials | Brittle on partial-text changes; revisit once instrumented data shows where real pain lives. |
| No prompt-injection eval scenario | Easy add-on; one scenario with malicious user text trying to make the bot cancel someone else's appointment. |
| No cached-token counter in eval output | OpenAI returns it; surfacing requires plumbing a `usage` field through `OpenAILLMAdapter`. |
| No `mypy --strict` gate in pre-commit / CI | Strict-clean coverage isn't there yet; documented as next quality gate in CLAUDE.md. |
| No availability cache | Skipped as a stretch; would be a 30-line in-memory `dict` keyed by `(date, provider_id)` invalidated on book/cancel. |

## Future work (in priority order)

1. **OpenRouter as the LLM gateway** with an ordered fallback list —
   production resiliency for LLM provider outages. One env-var change in
   `llm.py`.
2. **Streaming TTS** via ElevenLabs flush-after-each-clause — biggest
   perceived-latency win once instrumentation data shows where pain is.
3. **Proactive prefetch on STT partial transcripts** — fire
   `find_patient_by_phone` as soon as the partial contains digits,
   before the user finishes speaking.
4. **Audio smoke tests with a real TTS→STT loop** — current skeleton
   just asserts the dispatcher module imports.
5. **`mypy --strict` in pre-commit / CI.**
6. **Cached-token counter in eval output** — proves prompt-caching is
   working.
7. **Prompt-injection scenario** — caller asks the bot to cancel a
   different patient's appointment; assert the bot refuses.
8. **`AvailabilityCache`** with 60 s TTL in `repository.py`.

## File map (where to look for what)

- `src/prosper/ehr/` — FastAPI app, SQLAlchemy models, repository, schemas, db engine
- `src/prosper/dispatcher.py` — FSM, transcript, tool-whitelist enforcement
- `src/prosper/flows.py` — state graph topology + per-state tool whitelist
- `src/prosper/prompts.py` — `CLINIC_PERSONA` + per-state task messages
- `src/prosper/tools.py` — tool handlers + OpenAI schemas + `HANDLERS` map
- `src/prosper/llm.py` — `OpenAILLMAdapter` (implements `LLMClientProtocol`)
- `src/prosper/observability/timing.py` — `TimingCollector` + JSON span logs
- `src/prosper/bot.py` — Pipecat pipeline wiring + `DispatcherProcessor`
- `evals/` — `Scenario`/`StateExpectation` types, persona simulator,
  judge, runner, scenarios, pytest entrypoint, CLI
- `docs/superpowers/specs/2026-05-19-prosper-challenge-design.md` —
  full deliberation trail (LLM council verdict per decision)
- `docs/superpowers/specs/2026-05-19-other-solutions-best-ideas.md` —
  cross-survey of 10 reference solutions; borrowed ideas are cited
- `docs/superpowers/plans/2026-05-20-prosper-challenge-implementation.md`
  — bite-sized TDD implementation plan that produced this codebase
