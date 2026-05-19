# Survey of Other Prosper Challenge Solutions — Best Ideas

> Scope: 10 reference solutions under `other solutions/`. Goal is to extract **concepts** worth borrowing, not code. Source cited beside each idea so the candidate can verify.

Important caveats up front:
- `rahulharikumarr_prosper-challenge` is **not** a Prosper Health solution. It is a Warp Freight voice bot using the same Pipecat scaffold. Ignored for EHR/flow ideas; one infra pattern (Redis session store with in-mem fallback) is still worth noting.
- `jordigb4_prosper-challenge` ships a `tools.py` whose `initialize_tools()` body is essentially empty (one `FunctionSchema` with no description, no params, no handler). Treat its SOLUTION.md as concept-only — the code does not back the doc.
- `AlexLopezGomez_prosper-challenge` is the candidate's own prior submission. Included for completeness, but the goal is to find ideas in the *others*.

## Summary table

| Solution | EHR stack | Flow approach | Has evals? | Notable strengths | Notable weaknesses |
|---|---|---|---|---|---|
| AlexLopezGomez | Healthie via session-harvested GraphQL (Playwright fallback documented) | Pipecat Flows, 3-layer confirm gate | Yes — unit + prompt + adversarial + bench_latency + flow-transition + nodes-guard | Phased plan.md → SOLUTION.md, latency table in doc, 3-layer enforcement, stored-injection eval, discovery-gate spike retained as artifact | Single-clinic, Healthie-specific, no dual-provider LLM |
| MarioW333 | Healthie via Playwright + persistent Chromium profile (2FA bypass) | Single fat system prompt + 4 tools | No code evals (level-1..4 sketched in doc) | Aggressive parallelism: login overlaps greeting; create_patient runs in background while bot asks DOB; profile prefetch tab; `slot_taken` / `already_booked` structured tool results; language autodetect | Module-level singletons (`_page`, `_current_patient_id`) break under concurrency; never reset between calls; selectors are silent failure points |
| NoelDNathan | Healthie via Playwright; `playwright codegen` to author selectors | Custom direct-function flow (no Flows) | Live integration tests (`@pytest.mark.live`) | Honest dev-log of Healthie's UI changes (verification code added/removed mid-build); Gmail OTP reader; conversation transcript in SOLUTION.md as a real artifact; suggests `Result<Ok,Error>` lib for richer return types | No state-machine; direct functions; flow logic ad-hoc |
| PauMinguet | **Self-hosted FastAPI + SQLModel + SQLite** EHR (the only solution that fully met the "build a small EHR" brief) | Pipecat Flows graph: greet → register/identify → choose_action → pick_slot/pick_appointment → confirm_* → end | **Yes — full headless eval harness** (HeadlessFlow + persona simulator + LLM judge + state-assertion + 13 scenarios + baseline/regression diff + JSON output + token-cache reporting) | Eval rigour, ASGITransport for in-process EHR, pre-seeded slot rows (admin-blockable), idempotent POST /appointments via (slot_id + cancelled_at IS NULL), declarative `Scenario` dataclass | No TZ handling, single-provider, "double-confirm on single-appt cancel" UX wart |
| ericsorides | Healthie via Playwright (DOM-heavy) | Single system prompt + tools | None | Detailed write-up of selector pain (modal scoping, Escape to dismiss datepicker, double-click time field workaround, `form.requestSubmit` trick); honest about brittleness | Selector-fragile; no evals; no flow framework |
| ericvg97 | Healthie via Playwright | **Pipecat Flows** with intermediate "looking you up" speaking node | Has `test_healthie.py` (live) + `conftest.py`, no LLM evals | Uses `dateutil` for DOB parsing in handler (vs LLM structured output); intermediate node pattern to mask tool latency; explicit `respond_immediately=True` | No availability check; Playwright only; tool errors collapse to a generic end-node |
| jordigb4 | Healthie via Playwright (`healthie.py` skeleton) | Intended: state machine. Actually: `tools.py` empty | Has `test_backend.py` | SOLUTION.md is honest about scope: chose Flows for hallucination reduction, gpt-4o-mini for "name disambiguation" sub-call, listed real reliability gaps | Implementation doesn't back the doc; tools not registered |
| origovi | Healthie via Playwright | Single system prompt + tools | None | Very short, very honest SOLUTION.md (admits "agent gets interrupted during tool calls"); good list of known limitations | Nothing novel; baseline-quality solution |
| spagnoloe | Healthie via **GraphQL staging API** (free) — Playwright kept as documented fallback | Pipecat Flows (`app/scheduling/nodes.py`); decoupled `app/shared/tools/` abstraction | Unit tests for handlers + tool-function tests + manual E2E scripts; pre-commit (ruff+mypy) + GH Actions CI | Cleanest package layout (`app/scheduling`, `app/shared/tools`, `app/integrations`); EHR-agnostic tool layer; CLAUDE.md with hard rules ("fix root causes, never silence"); justifies why mocked Playwright unit tests are worse than nothing | Tools coupled to Flows handlers; staging endpoint relies on free tier surviving |
| rahulharikumarr | **(Not a Prosper solution — Warp Freight)** | Single prompt | None | Redis session store with in-memory fallback; SMS confirmation via Twilio; Railway deploy docs; explicit concurrency story | Wrong challenge — ignore for EHR ideas |

## Solution-by-solution highlights

### AlexLopezGomez (the candidate's prior submission, for reference only)
- Best idea: **Discovery-gate spike retained as a reviewer artifact** — `scripts/capture_healthie.py` writes `gate_report.json` deciding variant C vs A. Avoids committing to a path that the bot needs to walk back.
- Best idea: **3-layer enforcement of the confirmation gate** — graph topology + transition-only state writes + handler assert. Defense in depth so any one slip doesn't book the wrong patient.
- Best idea: **Stored / cross-turn prompt-injection eval** distinct from direct injection. The realistic attack is injected text that lives in context across turns, not text the user says immediately before a tool call.
- Best idea: **Latency table in SOLUTION.md** with p50/p95 per span from `bench_latency.py`. Turns "feels fast" into numbers.
- Anti-pattern to avoid: section-heavy plan.md is great for thinking, ugly for reviewers. Compress into SOLUTION.md.

### MarioW333
- Best idea: **Login-overlaps-greeting** + **create_patient-overlaps-DOB-collection** + **profile-prefetch-tab-overlaps-time-collection**. Treats every patient utterance as latency budget for the next Playwright op. New-patient flow gets ~20 s of dead air collapsed to near-zero because the bot is asking the next question while Healthie chews on the previous one.
- Best idea: **Persistent Chromium profile (`save_session.py`) to skip 2FA** on a "trusted device" — production-correct workaround for email-OTP login flows.
- Best idea: **Structured tool-result enum** (`slot_taken` / `already_booked` / `{patient_id, date, time}`) drives LLM branching cleanly. Better than free-text errors.
- Best idea: **`wait_for_load_state("networkidle")`** instead of fixed `wait_for_timeout(2000)` — adaptive wait that costs the real response time, not the worst-case.
- Anti-pattern to avoid: **module-level singletons** (`_page`, `_current_patient_id`, `_creation_task`) never reset between calls. SOLUTION.md explicitly admits this would book wrong-patient under concurrent calls. Don't ship this in 2026.

### NoelDNathan
- Best idea: **`playwright codegen` to author selectors interactively** — captured in the doc as the productivity unlock, not just an offhand mention.
- Best idea: **Honest dev-log of Healthie UI churn** ("2026-02-18 they added 2FA, 2026-02-20 they removed it") — signals real engineering instinct over generic process talk.
- Best idea: **Suggesting the `result` Python library** (`Result<Ok, Error>`) as the proper abstraction for tool return types. Cleaner than `None | dict | list`.
- Best idea: **Including a real example transcript** at the bottom of SOLUTION.md — the failure modes (STT misreading "Bosch" / "Bosque", LLM endlessly asking for confirmation) are far more illuminating than any latency claim.
- Anti-pattern: ad-hoc direct functions instead of Pipecat tools or Flows. Author admits "faster to iterate" but the result is a flow that's hard to test.

### PauMinguet — **the deepest eval rig in the survey, study this one carefully**
- Best idea: **`evals/runner.py` HeadlessFlow** — replays the same `NodeConfig` graph from `flow.py` but bypasses Pipecat. Each step calls OpenAI directly with the current node's tools, runs handlers in-process. **EHR runs in-process via `httpx.ASGITransport`** — no separate uvicorn needed. This is the cleanest answer to "how do you test a voice agent's logic without paying for STT/TTS or fighting WebRTC."
- Best idea: **Persona-driven scripted "patient" LLM** (`evals/sim.py`) drives the conversation; **judge LLM** scores transcript against per-scenario rubric. 13 scenarios across happy/recovery/adversarial.
- Best idea: **Declarative `Scenario` dataclass** with `persona`, `setup` (DB seeding closure), `expected_state` (post-call EHR assertion via SQLModel), and `judge_criteria` (list of natural-language pass conditions). Scenarios live in `evals/scenarios.py` as plain data — extremely easy to extend.
- Best idea: **Persona prompts that script the mistake explicitly** — e.g., `"say literally: 'My name is Daniel O-S-U-L-L-I-V-I-N' (note the I instead of A)"`. The persona is a deterministic actor with a scripted mis-step, then a correction. Tests the read-back-and-correct UX path that's otherwise impossible to exercise without humans.
- Best idea: **`--baseline previous.json --json results.json`** flow — eval suite emits structured results so a CI gate can diff against the prior run and exit non-zero on regression.
- Best idea: **Cached-token counter per scenario** in the output table — proves prompt-caching is actually working (75–85% hit rate reported), not just claimed.
- Best idea: **Known-limitations section that lists eval failure modes** ("the judge was wrong about DOB confirmation in one run"; "persona LLM drifted from scripted intent"). Demonstrates the candidate understands the eval is a sampling, not a proof.
- Best idea: **Pre-seeded slot rows vs computed availability** — each slot is a row, booking is a FK; admins can block lunch breaks by inserting an exception row. Closer to how real clinics operate, and turns idempotency into a unique constraint.
- Best idea: **Idempotent `POST /appointments`** — checks for active appointment on same slot first; same patient + same slot → returns existing row (handles LLM retries). Different patient → 409. No distributed locks needed.
- Best idea: **`CLINIC_PERSONA` is a long, stable preamble** (≥1024 tokens for OpenAI prompt cache); per-node `task_messages` are short and specific. Architects the prompt for caching, not just clarity.
- Anti-pattern: relying on the LLM to do `dob: YYYY-MM-DD` normalization via the tool description. Works for English DOBs; brittle for "second of February of seventy-eight."

### ericsorides
- Best idea: **Modal scoping with `[data-testid="modal-content"]`** — avoids picking up the wrong `role=dialog` (e.g., a chat widget). The class of bug nobody anticipates until it happens.
- Best idea: **`form.requestSubmit(btn)` + 480 ms second click** to defeat Healthie's broken submit handling. Pragmatic, ugly, documented honestly.
- Best idea: writing up the **timezone gotcha you can't see** — same 11:00 displayed in both EDT and CEST UIs but stored as different absolute instants. (This came from AlexLopez's solution actually — but ericsorides also notes "if Healthie session expires mid-call we re-login.")
- Anti-pattern: no flow framework, single fat prompt enforces "Initial Consultation 60min vs Follow-up 45min" via prose. LLM ignores it more often than the SOLUTION.md admits.

### ericvg97
- Best idea: **Intermediate "looking you up" speaking node** with `respond_immediately=True` — fills the tool-call gap with a spoken filler that's part of the conversation graph, not a `TTSSpeakFrame` hack.
- Best idea: handler-side `dateutil.parser.parse(date_of_birth)` with a try/except routing back to `greet_and_collect` on failure. Cheap recovery loop without state.
- Anti-pattern: collapses *all* lookup errors into a single "patient not found" end-node. No retry, no spell-back, no escalate path.

### jordigb4
- Best idea (SOLUTION.md only): **gpt-4o-mini for a name-disambiguation sub-call** inside the bot, separate from the main turn-handling model. "Mini-LLM for a tightly-scoped sub-task" is a real pattern.
- Best idea (SOLUTION.md only): single-LLM-eval-with-customer-persona — described before reading Pau's actual implementation. Convergent thinking.
- Anti-pattern: ships an empty `tools.py`. The plan beats the execution by a wide margin; reviewers will notice.

### origovi
- Best idea: **Honest cuts list** — "If user talks while the agent is executing a function, it gets interrupted." "The agent does not inform the user before calling a function, resulting in an awkward silence." These are the *real* product problems that other solutions paper over.
- Anti-pattern: every tool call re-logs into Healthie. ~10–20 s every turn. Whole bot is unusable in practice.

### spagnoloe
- Best idea: **Cleanest project layout in the survey** — `app/scheduling/{nodes,handlers,prompts}.py` + `app/shared/tools/` + `app/integrations/{healthie_api,healthie_playwright}.py`. The tool layer is EHR-agnostic; swapping Healthie for another EHR means changing files in `integrations/`, not the conversation graph.
- Best idea: **`CLAUDE.md` with hard repo rules** ("Fix root causes — never patch symptoms or silence errors", "Include tests"). Production-grade self-discipline.
- Best idea: **GraphQL via the freely-available staging endpoint** (`staging-api.gethealthie.com/graphql`) — no key required, no Playwright. Sidesteps the entire "session harvest vs Playwright" debate.
- Best idea: **Justifies the absence of mocked Playwright unit tests** — "mocked tests pass when the mock matches our assumptions; they fail to catch when our assumptions no longer match reality." Better than no tests + no rationale.
- Best idea: **Keeps Playwright code in the repo as documented fallback** so the API-down failure mode has a real ladder rung.
- Best idea: **OpenRouter as the LLM gateway** for failover — single env-var swap, OpenRouter handles tool-format translation and offers `:nitro` / `:exacto` routing variants. Much cheaper than building a `FallbackLLMService` from scratch.
- Anti-pattern: Pipecat-flows handlers import tools directly — the "decoupled tool layer" claim is partially aspirational.

### rahulharikumarr (Warp Freight — wrong challenge but one borrowable idea)
- Best idea: **Redis session store with in-memory fallback** (`session_store.py`) — production-ready pattern; falls back to local dict if `REDIS_URL` unset. Useful for any "callback state lives in shared infra in prod, in process in dev" need.

## Cross-cutting best ideas (worth borrowing the CONCEPT of, not the code)

### EHR / Backend
- **Build a real FastAPI + SQLModel + SQLite EHR** (Pau) rather than wrestling with Healthie. SQLModel = Pydantic + SQLAlchemy in one class. Auto-generated `/docs` makes curl-testing trivial. Production switch to Postgres = one env var.
- **Pre-seeded slot rows** (Pau) instead of computed availability. Lets ops block lunch breaks / PTO as row exceptions; idempotency becomes a unique constraint.
- **Idempotent create_appointment** (Pau): same patient + same slot → return existing row; different patient → 409. No distributed locks.
- **One small extension to the EHR API** documented as deliberate (Pau added `GET /patients/{id}/appointments` returning slot start_at inlined so the cancel flow can read the appointment time back). Better than secretly stretching the spec.
- **Persistent Chromium profile to bypass 2FA** (MarioW333) is the production-correct workaround when no API key is available.
- **GraphQL via staging endpoint** (spagnoloe) sidesteps both the session-harvest and Playwright paths entirely if you can live with staging data.

### Conversation flow
- **Pipecat Flows over single prompt** (5+ solutions converged here) — per-node tool gating turns "agent did the wrong thing at the wrong time" from a prompt-obedience problem into a topology problem.
- **Two-step confirm-before-mutate** (Pau): separate `pick_slot → confirm_booking` node and `pick_appointment → confirm_cancel` node. Eliminates the "agent skipped confirmation" failure mode and works around an LLM bug Pau observed (looping on cancel without committing the tool call).
- **Intermediate "looking you up" speaking node** (ericvg97, ericsorides) — fills tool-call latency with conversation-graph speech, not a `TTSSpeakFrame` patch.
- **Structured tool-result enums** (MarioW333: `slot_taken` / `already_booked` / success) drive LLM branching deterministically. Better than free-text errors.
- **LLM-side normalization in tool descriptions** (Pau: `"Convert 'April third nineteen ninety-two' to '1992-04-03'"`) avoids dragging in `dateparser`.
- **Long stable persona preamble for prompt-cache hits** (Pau: ≥1024 tokens, 75–85% cache hit reported). Architect the prompt for caching from day 1.

### Eval suite
- **`HeadlessFlow` that replays the Flows graph without Pipecat** (Pau) — the killer pattern. Calls OpenAI directly with each node's tools, runs handlers in-process. Decouples LLM-behavior testing from the audio pipeline entirely.
- **EHR in-process via `httpx.ASGITransport`** (Pau) — no separate uvicorn for the EHR during evals. Combine with `reset_db_to_slots_only()` per scenario for hermetic runs.
- **Persona-driven simulator + LLM judge** (Pau, also planned by jordigb4 and spagnoloe) — declarative scenarios where the persona's mistake is scripted explicitly (e.g., spell name wrong on first attempt, correct on read-back).
- **State assertion on the EHR after each scenario** (Pau) — `expected_state` checks patient_count, active_appointment_count, cancelled_appointment_count. Catches "judge said yes but no appointment was created" gap.
- **`--baseline previous.json` regression diff** (Pau) — CI gate exits non-zero on drift.
- **Cached-token counter per scenario** (Pau) — turns "we cache prompts" into a measurable signal.
- **Stored / cross-turn injection eval** (AlexLopez) — the realistic attack surface is injected text in context across turns, not direct prompts.
- **Three-layer eval taxonomy**: unit (mocked) + scenario (LLM judge) + adversarial (no-state-change assertion). Almost everyone with evals converged on this.
- **Live integration tests gated by a `@pytest.mark.live` marker** (NoelDNathan) so they only run with credentials, never in regular CI.

### Latency
- **Parallelize Healthie ops with patient utterances** (MarioW333): login overlaps greeting; create_patient overlaps DOB collection; profile-prefetch tab overlaps time-of-day collection. Treats every spoken turn as latency budget.
- **`wait_for_load_state("networkidle")`** over fixed `wait_for_timeout` (MarioW333) — adaptive waits cost real latency, not worst-case.
- **Filler-speech via intermediate node** (ericvg97) is cleaner than `TTSSpeakFrame` hacks.
- **`language_code="en"` on Realtime STT** (Pau) skips auto-detect — modest win for English-only clinics.
- **Per-node model selection** (Pau plan, jordigb4 plan): cheap model for transition-only nodes, capable model for free-form turns. Half a day of work; not always worth it.
- **Latency table with p50/p95 per span pasted into SOLUTION.md** (AlexLopez) — converts a claim into a measurement.

### Reliability
- **OpenRouter as the LLM gateway with ordered fallback list** (spagnoloe) — much cheaper than building a `FallbackLLMService` class; also translates OpenAI tool schemas to other providers.
- **GraphQL → Playwright fallback ladder** (spagnoloe, AlexLopez) — keep both implementations available; detect via consecutive API errors.
- **Verify-don't-retry on write timeout** (AlexLopez plan): issue an `appointments(patient_id, date)` read to check whether the write landed. Avoids duplicate bookings on lost responses.
- **Login circuit-breaker** (AlexLopez): 2 login failures / 5 min → 60 s open. Prevents account lockout from a misbehaving bot.
- **Healthie session expiry mid-call** (MarioW333) — `ensure_login()` wrapper that checks if a background `_login_task` is still running and waits for it.
- **Pre-recorded `TTSSpeakFrame` LLM-down path with regex phone capture** (AlexLopez) — no LLM in the loop. Captures `\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b`, appends to a callbacks log, disconnects.
- **`Result<Ok, Error>` return type for integrations** (NoelDNathan) — richer error signal than `None | dict`.

### Project structure
- **`app/scheduling/{nodes,handlers,prompts}.py` + `app/shared/tools/` + `app/integrations/`** (spagnoloe) is the cleanest layout. The tool layer is EHR-agnostic; the conversation graph doesn't know about Healthie.
- **`CLAUDE.md` with explicit repo rules** (spagnoloe): "fix root causes, never silence errors", "include tests". Self-discipline as a committed artifact.
- **Pre-commit hooks (ruff + mypy) + GH Actions CI** (spagnoloe) gates lint/type/test on every PR. Excludes live integration scripts from CI.
- **`plan.md` (phased thinking record) → `SOLUTION.md` (the canonical reviewer doc)** (AlexLopez) — keep the process visible without burying the conclusion.
- **`evals/{runner,sim,scenarios,run}.py`** (Pau) — eval suite as a real Python package with its own runner CLI (`--only`, `--tag`, `-v`, `--json`, `--baseline`).
- **Discovery-gate script retained as reviewer artifact** (AlexLopez: `scripts/capture_healthie.py` outputs `gate_report.json`) instead of being a throwaway spike.

### Other clever ideas
- **Gmail OTP reader** (NoelDNathan's `utils/get_verification_code.py`) for Healthie's verification-code login. Niche but real.
- **`playwright codegen <url>`** (NoelDNathan) as the recommended way to author selectors. Productivity multiplier.
- **Redis session store with in-memory fallback** (rahulharikumarr) for callback queues / per-call state shared across instances.
- **Twilio SMS confirmation post-booking** (rahulharikumarr) closes the "voice promise → written record" loop.
- **Healthie returns HTTP 200 with `UNAUTHORIZED` in errors[].extensions.code**, not 401, when the bearer expires (AlexLopez live-integration finding). Plus introspection is disabled server-side. Both are field intelligence worth retaining.
- **Filter captured GraphQL requests for ones carrying an `Authorization` header** (AlexLopez) — naively grabbing the "first GraphQL request" gets you Healthie's unauth bootstrap calls.

## Anti-patterns observed (do NOT do)

- **Module-level singletons for browser / state** (MarioW333, base scaffold). Breaks under concurrency, leaks across calls. Pass session through pipeline instead.
- **Never resetting state in `on_client_disconnected`** (MarioW333 admits) — next caller inherits previous patient_id. Wrong-patient incident waiting to happen.
- **Selectors hardcoded with no daily CI smoke test** (everyone Playwright-based). Healthie ships a UI change → silent break. At minimum, a `@pytest.mark.live` selector smoke that runs nightly.
- **Logging into Healthie on every tool call** (origovi). 10–20 s of dead air per turn. Always reuse session.
- **Empty `tools.py` shipped with a polished SOLUTION.md** (jordigb4). The plan-vs-code gap is what reviewers grade.
- **Direct functions instead of Pipecat tool registration** (NoelDNathan). Faster to iterate, much harder to test. Wrong trade for a senior submission.
- **Collapsing all tool errors to a single "end" node** (ericvg97). No retry, no spell-back, no escalate.
- **Single fat system prompt enforcing every business rule via prose** (ericsorides). LLM ignores it more often than the SOLUTION.md admits. Topology > prompt obedience.
- **No state-assertion in evals — only judge** (anyone who only does LLM-as-judge). The judge can hallucinate success on a transcript where nothing actually happened in the EHR. Always combine.
- **Plan that out-runs the implementation** (jordigb4, somewhat AlexLopez's 600-line plan.md). Compress to SOLUTION.md.
- **Hardcoded "I can speak the following languages" instead of relying on STT/LLM language detection** (most solutions). MarioW333's auto-detect on first response is the right pattern.
- **Bimodal latency UIs** (AlexLopez explicitly rejected the GraphQL-read / Playwright-write hybrid for this reason). If half your turns are 200 ms and half are 5 s, the call feels broken even with filler.

## Ideas to consider integrating into our spec (`2026-05-19-prosper-challenge-design.md`)

Concrete, citation-backed candidates the next brainstorming round should weigh:

1. **Adopt Pau's HeadlessFlow eval architecture.** Replay the same `NodeConfig` graph from `flow.py` outside Pipecat, run EHR in-process via `httpx.ASGITransport`. This is the single highest-leverage idea in the survey. Source: PauMinguet `evals/runner.py`.
2. **Declarative `Scenario` dataclass with `(name, tags, persona, setup, expected_state, judge_criteria, max_turns)`.** Make scenarios plain data so adding one is a 20-line PR, not an eval-framework refactor. Source: Pau `evals/scenarios.py`.
3. **Persona prompts that script the mistake.** Don't ask the persona LLM to "make a mistake"; tell it literally what to say wrong and when to correct. Source: Pau `recover_from_name_typo`, `dob_correction`, `phone_digit_correction`.
4. **State-assertion + judge** as paired post-conditions (e.g., `patient_count`, `active_appointment_count`, `cancelled_appointment_count`). Catches the judge-said-yes-but-nothing-happened gap. Source: Pau `StateExpectation`.
5. **`--baseline previous.json --json results.json` regression diff** so CI can fail on drift. Source: Pau `evals/run.py`.
6. **`@pytest.mark.live` marker for Healthie-touching tests** so unit/scenario CI stays hermetic. Source: NoelDNathan.
7. **`Result<Ok, Error>` return type for integration functions** (or a typed `TypedDict` with `ok | error | reason`). Source: NoelDNathan, MarioW333's `slot_taken`/`already_booked` enums.
8. **Parallelize Healthie ops with patient utterances** as a first-class section in SOLUTION.md (login overlaps greeting, etc.). Source: MarioW333.
9. **Persistent Chromium profile (`save_session.py`) to bypass 2FA** — even if we go GraphQL-only, keep this as the Playwright-fallback story. Source: MarioW333.
10. **OpenRouter as the LLM-fallback story** — single env-var swap + ordered fallback list + `:nitro`/`:exacto` routing. Replaces our "P2 Anthropic adapter" plan. Source: spagnoloe.
11. **Long stable `CLINIC_PERSONA` preamble ≥1024 tokens** so OpenAI prompt cache kicks in; per-node `task_messages` short. Cite the 75–85 % cache hit rate in SOLUTION.md. Source: Pau.
12. **Spagnolo's `app/scheduling/` + `app/shared/tools/` + `app/integrations/` layout** as the project structure target. Source: spagnoloe.
13. **`CLAUDE.md` at repo root with hard rules** (fix root causes, include tests, separation of concerns). Signals self-discipline. Source: spagnoloe.
14. **Two-step confirm-before-mutate as a graph pattern** (`pick_slot → confirm_booking`, `pick_appointment → confirm_cancel`). Source: Pau.
15. **Idempotent `POST /appointments` via active-slot check** instead of distributed locks. Source: Pau.
16. **Pre-seeded slot rows** with admin-blockable exceptions. Source: Pau.
17. **Real example transcript in SOLUTION.md** showing the bot's actual behavior including failures. Source: NoelDNathan. Counterintuitively the most impressive thing you can include.
18. **Honest dev-log section** documenting what Healthie did during the build week. Source: NoelDNathan ("2026-02-18 they added 2FA, 2026-02-20 they removed it").
19. **Cached-token counter in eval output table** to prove prompt-caching is working. Source: Pau.
20. **Filter captured GraphQL requests by `Authorization` header presence** in the discovery gate. Source: AlexLopez live-integration finding (already in our spec; reinforce).

The strongest single-borrow is Pau's eval architecture. The strongest *layout* borrow is spagnoloe's `app/` package split. The strongest *latency* borrow is MarioW333's overlap-Healthie-with-utterances pattern.
