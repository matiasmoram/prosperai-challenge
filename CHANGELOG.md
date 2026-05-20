# Changelog

All notable changes to this submission, reverse-chronological. Versions are
loose semver tags grouped by delivery wave rather than published artifacts —
this repo is a single interview submission, not a released package.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).

## [0.1.3] — 2026-05-20 — Offline eval, dev-loop polish, repo hygiene

### Added

- **Mock LLM eval mode** — `evals/mock_llm.py` (~700 LOC deterministic
  mock) + `make mock-eval` target. Runs all 16 scenarios with no
  `OPENAI_API_KEY` in ~5 s; `test_scenario_mock` is parametrised over
  every scenario so the suite gates on a real run on every push.
- **Live-eval gating on `PROSPER_EVAL_LIVE=1`** — previously a missing
  quota silently passed; the live suite now exits with code 2 unless
  both `PROSPER_EVAL_LIVE=1` and `OPENAI_API_KEY` are set.
- **5 more adversarial / mixed scenarios** — `multi_turn_drift_hallucinated_slot`,
  `phone_format_chaos`, `patient_correction_mid_register`,
  `goodbye_mid_confirmation`, `insurance_question_redirect` (11 → 16).
- **Parallel eval runner** — `python -m evals --concurrency N` (default 4)
  with per-scenario isolated SQLite engine so transactional state never
  crosses scenarios.
- **`make verify`** — one-shot pre-submit gate: lint → format → mypy →
  pytest. Stops on first failure.
- **`make status`** + `scripts/status.py` — repo health snapshot (test
  count, coverage, dirty files, lint status).
- **`make mock-eval`** — see above.
- **Repo hygiene**: `CONTRIBUTING.md` (how to add scenarios / tools /
  states), `SECURITY.md` (SSRF guard, PII redaction, threat model),
  `CHANGELOG.md` (this file), `.editorconfig`, `.gitattributes`.
- **Docs expansion**: `docs/glossary.md` (terminology cheat-sheet),
  `docs/architecture.md` (process + FSM diagrams), `docs/interview-notes.md`
  (candidate prep + decision evidence), three ADRs under `docs/adr/`,
  `docs/bench-results.md` (pinned bench snapshots).

### Changed

- `SOLUTION.md` reconciled with the new file map, eval-suite section,
  quality-gates row (149 tests + 17 skipped, 91% coverage on
  `src/prosper`, 97% excluding `bot.py`), and trimmed future-work list.
- `README.md` quickstart now leads with `make mock-eval` as the zero-cost
  way to verify the suite; `make` table grew `mock-eval`, `verify`,
  `status` rows.

### Fixed

- Scenario table in `SOLUTION.md` now lists all 16 scenarios (was 11);
  test count in `README.md` corrected from 125 → 149.

## [0.1.2] — 2026-05-20 — Security, coverage, parallel eval

### Added

- **SSRF guard** at startup: `PROSPER_EHR_URL` must be `http`/`https` with a
  hostname or the bot exits before importing pipecat (`src/prosper/bot.py`).
- **PII redaction layer** for tool results destined for the LLM — UUIDs are
  replaced with human-readable summaries so leaked transcripts are cheaper to
  contain (`src/prosper/dispatcher.py::_redact_for_llm`,
  `src/prosper/observability/redact.py`).
- **DoS caps** on tool result sizes and EHR pagination to prevent a
  pathological dataset from exploding the context window.
- **+79 coverage tests** across dispatcher gaps, tool errors, EHR client
  failure modes, redaction, and timing — repo coverage now 91 %.
- **Parallel eval runner**: `python -m evals` accepts `--concurrency N` and
  runs scenarios in a thread pool, cutting full-suite wall-clock ~3×.
- **5 new adversarial scenarios** in `evals/scenarios.py` (hallucinated
  confirmation, mid-call intent switch, ambiguous DOB, no slots, repeated
  no-match).
- **UX polish**: clearer copy in CONFIRM_BOOK / CONFIRM_CANCEL,
  filler-speech consistency across all tool-firing states.
- **CI matrix** Python 3.10 + 3.11, coverage upload, bench-smoke job
  (`.github/workflows/`).
- **`env.example`** documents `PROSPER_BOT_ENTRYPOINT` and other vars.

### Changed

- `SOLUTION.md` rewritten to v4 reflecting the security wave.
- README updated with the new env vars and the eval CLI.

### Fixed

- `--concurrency` arg was parsed but not threaded into `_run_all`.

### Commits

- `ed45435` ci: matrix py3.10/3.11, coverage upload, bench-smoke job, hardening
- `bf8727a` docs+env: env.example docs PROSPER_BOT_ENTRYPOINT, README updates, gitignore coverage.xml
- `2851424` fix: --concurrency arg threaded into _run_all call
- `3850fb5` feat: wave 3 — parallel-eval refactor, +5 scenarios, UX polish, SOLUTION.md v4
- `45ba2d5` sec+test: SSRF guard, PII redaction, DoS caps, +79 coverage tests

## [0.1.1] — 2026-05-20 — Performance wave + reliability

### Added

- **TTFT instrumentation** end-to-end with `cached_prompt_tokens` surfaced
  in transcript logs so prompt-cache regressions show up immediately
  (`src/prosper/observability/timing.py`).
- **Reliability layer**: retry-with-jitter on transient EHR errors, primary
  → fallback model swap (`PROSPER_BOT_FALLBACK_MODEL`), structured error
  taxonomy via `Result[Ok, Err]`.
- **ElevenLabs Flash v2.5** for STT/TTS, shaving ~150 ms median TTFT.
- **Filler speech** ("one moment", "let me check") injected before every
  tool-firing state to mask EHR round-trip latency without burning LLM
  tokens.
- **Adversarial eval scenarios** (×5) + a `hallucinated_confirmation` regex
  catching the LLM saying "you're booked" before `create_appointment` fired.
- **Re-runnable bench CLI** `scripts/bench.py` + `make bench` target,
  pinned snapshot in `docs/bench-results.md` for cross-wave regression
  diffs.
- **mypy --strict** gate, expanded ruff rule set, three ADRs documenting
  hybrid FSM / separate EHR process / paired state+judge eval,
  `docs/architecture.md`, `docs/interview-notes.md`.
- **Dockerfile** + `.dockerignore` + `scripts/README.md` for reproducible
  packaging.

### Changed

- SQLite pragmas: WAL mode + `expire_on_commit=False` for ~2× speedup on
  the EHR endpoints under the eval suite.
- VAD (silero) is now lazy-imported behind the env fail-fast guard,
  removing it from `--help` startup cost.
- `list_available_slots` N+1 query collapsed into a single join.
- Tests run ~30 % faster after session-scoped EHR fixture + in-memory DB.

### Fixed

- A1–A4 from the codebase-audit subagent (off-by-one in pagination, stale
  memory key in cancel flow, incorrect error code mapping, duplicate state
  log entry).

### Commits

- `a6ef2e9` perf: SQLite WAL + expire_on_commit=False + lazy VAD + env fail-fast
- `33f0a24` docs: bench-results.md — pinned snapshot of EHR endpoint p50s for regression diffs
- `ac415e5` build: make bench target for scripts/bench.py
- `2059e28` build+docs: mypy strict gate, expanded ruff rules, ADRs, architecture, interview prep
- `961330f` fix: prod-readiness wave (subagent #1)
- `36c50b1` build: .dockerignore + scripts/README.md
- `d089724` test: scripts/bench.py — re-runnable EHR endpoint benchmark CLI
- `8958fdd` perf: faster tests + fix N+1 in list_available_slots
- `df5dc61` feat: filler speech in tool-firing states + SOLUTION.md v3
- `b99485a` feat(eval): 5 adversarial scenarios + hallucinated-confirmation regex
- `99b203e` feat: reliability layer (README bonus #2) + ElevenLabs Flash v2.5
- `9348756` fix: audit bugs A1-A4 from codebase-audit subagent
- `1859eea` feat: TTFT instrumentation + cached_prompt_tokens surfacing

## [0.1.0] — 2026-05-20 — Initial submission

The first complete vertical slice: a pipecat voice loop driven by an FSM
dispatcher that calls a FastAPI EHR over HTTP, with a deterministic eval
suite that pairs final-state assertion with an LLM judge.

### Added

- **EHR service** — FastAPI app, SQLAlchemy models, Pydantic schemas, DB
  factory, seed script with 3 patients + provider slots
  (`src/prosper/ehr/`).
- **Async EHR client** with typed `Result[Ok, Err]` returns
  (`src/prosper/ehr_client.py`, `src/prosper/result.py`).
- **Tool handlers** (`find_patient_by_phone`, `find_patient_by_name_dob`,
  `create_patient`, `list_availability_slots`,
  `get_upcoming_appointments`, `create_appointment`, `cancel_appointment`)
  with shared `HANDLERS` / `TOOL_SCHEMAS` registries
  (`src/prosper/tools.py`).
- **FSM dispatcher** — per-state tool whitelist, transition table driven
  by tool result codes + short-circuit keywords, conversational memory
  (`src/prosper/dispatcher.py`, `src/prosper/flows.py`).
- **Per-state prompts** — `CLINIC_PERSONA` preamble (~1100 tokens, cached)
  + task messages ≤ 1 KB each (`src/prosper/prompts.py`).
- **OpenAI adapter** with tool-call streaming
  (`src/prosper/llm.py`).
- **pipecat bot** — STT → dispatcher → TTS pipeline
  (`src/prosper/bot.py`, `bot.py`).
- **Eval suite** — paired state-assertion + LLM-judge scenarios,
  `HeadlessFlow` runner, CLI at `python -m evals`
  (`evals/scenarios.py`, `evals/runner.py`, `evals/__main__.py`).
- **CI** — GitHub Actions running ruff + pytest on every push, pre-commit
  hook config.
- `SOLUTION.md` + `README.md` reviewer-facing docs.
- `CLAUDE.md` with hard rules for LLM contributors.
- Audio smoke-test skeleton (`evals/audio_smoke/`).

### Commits

- `0a913d7` docs: SOLUTION.md + README.md
- `899793b` build: CLAUDE.md hard rules, pre-commit, GH Actions CI, audio smoke skel
- `0f67a62` feat(bot): pipecat pipeline driven by dispatcher
- `a48c20f` feat(eval): paired state+judge scenario suite + HeadlessFlow runner + CLI
- `7c4743c` feat: FSM dispatcher + prompts + flows + OpenAI adapter + timing
- `f857cde` feat: Result[Ok,Err] + async EHR client + tool handlers
- `005b9be` feat(ehr): FastAPI app + Pydantic schemas + DB factory + seed script
- `c580e50` feat(ehr): SQLAlchemy models + repository
- `4d27821` build: add FastAPI/SQLAlchemy/httpx/test deps + src/prosper scaffold

## [0.0.x] — 2026-05-19 — Pre-implementation

Design and planning notes only; no shipped code.

### Commits

- `d8f526a` docs: implementation plan for prosper challenge
- `b16021a` docs: spec v2 — integrate ideas from cross-survey
- `cd68778` docs: design spec for prosper challenge submission
- `c47d9a6` initial commit
