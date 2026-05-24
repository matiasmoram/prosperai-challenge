# Changelog

Chronological narrative of every change made to this repo, oldest → newest,
derived from `git log`. Forward-reading and grouped by delivery wave; each
entry is `commit-hash — what changed, and why`. Dates are
`America/New_York` from the commit metadata.

**Span:** 2026-05-19 → 2026-05-24 · 67 commits · 1 author.

---

## Phase 0 — Design & planning (2026-05-19)

No shipped code; spec and plan only.

- `c47d9a6` — initial commit.
- `cd68778` — design spec for the Prosper challenge submission.
- `b16021a` — spec v2; integrated ideas from a cross-survey of approaches.
- `d8f526a` — implementation plan.

## Phase 1 — Initial vertical slice → `0.1.0` (2026-05-20)

First end-to-end slice: pipecat voice loop → FSM dispatcher → FastAPI EHR
over HTTP, with a deterministic eval suite (paired state-assertion + LLM
judge).

- `4d27821` — repo scaffold + deps (FastAPI / SQLAlchemy / httpx / test).
- `c580e50` — EHR SQLAlchemy models + repository.
- `005b9be` — EHR FastAPI app, Pydantic schemas, DB factory, seed script.
- `f857cde` — `Result[Ok, Err]` type, async `EHRClient`, tool handlers.
- `7c4743c` — FSM dispatcher + per-state prompts + flows + OpenAI adapter + timing.
- `a48c20f` — eval suite: paired state+judge scenarios, `HeadlessFlow` runner, CLI.
- `0f67a62` — pipecat bot pipeline driven by the dispatcher.
- `899793b` — CLAUDE.md hard rules, pre-commit, GitHub Actions CI, audio-smoke skeleton.
- `0a913d7` — first reviewer-facing `SOLUTION.md` + `README.md`.

## Phase 2 — Performance + reliability wave → `0.1.1` (2026-05-20)

- `1859eea` — TTFT instrumentation; surface `cached_prompt_tokens` so prompt-cache regressions show up immediately.
- `9348756` — fix audit bugs A1–A4 (pagination off-by-one, stale cancel-flow memory key, wrong error-code mapping, duplicate state-log entry).
- `99b203e` — reliability layer (retry-with-jitter, primary→fallback model swap) + ElevenLabs Flash v2.5 for STT/TTS (~150 ms median TTFT cut).
- `b99485a` — 5 adversarial scenarios + `hallucinated_confirmation` regex (catch "you're booked" before `create_appointment` fires).
- `df5dc61` — filler speech in tool-firing states to mask EHR latency without burning tokens.
- `8958fdd` — faster tests + fix N+1 query in `list_available_slots`.
- `d089724` — re-runnable EHR bench CLI (`scripts/bench.py`).
- `36c50b1` — Dockerfile/`.dockerignore` + `scripts/README.md`.
- `961330f` — prod-readiness fixes (subagent #1).
- `2059e28` — mypy `--strict` gate, expanded ruff rules, ADRs, architecture doc, interview prep.
- `ac415e5` — `make bench` target.
- `33f0a24` — pinned bench snapshot (`docs/bench-results.md`) for cross-wave regression diffs.
- `a6ef2e9` — SQLite WAL + `expire_on_commit=False` + lazy VAD import + env fail-fast guard.

## Phase 3 — Security, coverage, parallel eval → `0.1.2` (2026-05-20)

- `45ba2d5` — SSRF guard on `PROSPER_EHR_URL`, PII redaction layer, DoS caps, +79 coverage tests (coverage → 91%).
- `3850fb5` — wave 3: parallel-eval refactor, +5 scenarios, UX polish, SOLUTION.md v4.
- `2851424` — fix: `--concurrency` arg was parsed but never threaded into `_run_all`.
- `bf8727a` — `env.example` documents `PROSPER_BOT_ENTRYPOINT`; README updates; gitignore `coverage.xml`.
- `ed45435` — CI matrix py3.10/3.11, coverage upload, bench-smoke job, hardening.

## Phase 4 — Offline eval + repo hygiene → `0.1.3` (2026-05-20)

- `0086913` — `CONTRIBUTING.md`, `docs/glossary.md`, `SECURITY.md`, `CHANGELOG.md`, `.editorconfig`.
- `a2ef125` — `make verify` = lint + format + mypy + tests in one shot.
- `e2f6435` — `.gitattributes` (`text=auto eol=lf`) to stop CRLF warning storm on Windows.
- `c1e5b26` — `scripts/status.py` + `make status` repo-health snapshot.
- `6e65439` — `--mock-llm` offline eval mode + `PROSPER_EVAL_LIVE` gate.
- `861f616` — `make mock-eval`: 16 scenarios offline, no API key.
- `02728b7` — SOLUTION.md + README.md + CHANGELOG.md reconciliation (v5).

## Phase 5 — Multi-subagent hardening sweep (2026-05-20)

Parallel subagent waves: bug-fuzzing, observability, code quality, prompt
hardening, concurrency review.

- `4ef04f4` — remove transient fuzz-probe scripts left by a subagent.
- `7f090ee` — multi-wave: 5 real bug-fuzz fixes (kwarg filtering, `_parse_dob` `fuzzy=False` data-corruption fix, idempotent cancel, `normalize_phone`/`normalize_name` edge cases) + observability (request-id propagation, `/metrics`, per-session/turn ids) + test-fixture dedup.
- `02ccfbc` — prompt hardening (adversarial-safety persona block, refusal patterns) + mutation-killing tests.
- `2297d9c` — async/concurrency review: 2 real bugs fixed (silent `_llm_turn` dead-air; `asyncio.gather` swallowing sibling-scenario crashes) + 6 paths verified-safe + regression tests.
- `d2450a0` — code-quality sweep: docstrings, dead-code removal.

## Phase 6 — Live-eval truth pass (2026-05-20)

Running against real OpenAI surfaced bugs the mock couldn't.

- `4db270b` — **real bug from live eval:** OpenAI tool-call schema compliance — assistant messages now carry `tool_calls`, tool responses use `tool_call_id`; missing-field calls return structured `Err` instead of crashing the turn.
- `18bbe28` — `ERRORS.md`: honest snapshot of live-eval failures (1 crash + 13 scenario state-assertion mismatches, with root causes).

## Phase 7 — Id contract fix + naturalness + parallel features (2026-05-23)

- `30bda0f` — **core live-booking fix:** `_redact_for_llm` was stripping `slot_id`s while the prompt told the LLM to remember them → hallucinated UUIDs → canned fallback. Introduced bracketed `[1]`/`[2]` handles + `_resolve_memory_handles`; history-orphan pruning (OpenAI 400 fix); abort edges on "no" at confirm states; menu-phrasing guard; no-match → REGISTER routing. Also bundles the operator console, specialty filter, RESCHEDULE_FLOW, FUTURE.md, ADR-004. CLAUDE.md hard rules 7→10. Mock-eval 18/18.
- `3266b02` — auto-migrate `providers.specialty` on boot (idempotent `ALTER TABLE`) so a stale `ehr.db` no longer 500s; seed docstring accuracy.
- `ed7eeb7` — call-style WebRTC frontend at `:7861/call` (avatar + audio-driven mouth); in-flight symptom-triage router, observers, adversarial test suite, EHR repo hardening. 337 tests green.

## Phase 8 — Eval coverage expansion (2026-05-23)

Driving mock-scenario coverage of every state, tool, and `Err`-code path.

- `29f6b04` — +26 mock scenarios; failure-mode coverage 18 → 44.
- `0a02922` — cover `date_unparseable` Err recovery (→ 50).
- `7e0e2b7` — cover `no_consecutive_slots` + `invalid_duration` Err paths (→ 52).
- `fa13f75` — cover `patient_exists` Err (duplicate-phone registration) (→ 53).
- `5a6540e` — multi-turn memory: re-list invalidates stale slot handle (→ 54).
- `8456f4e` — `medical_emergency` red-flag: emergency never becomes a booking (→ 55).

## Phase 9 — FUTURE-roadmap features (2026-05-24)

Working through ranked items in `FUTURE.md`.

- `44187a8` — EHR input-validation hardening on request schemas (OWASP A03): phone char-set/length guard, DOB plausibility bounds, HTML/script-tag stripping on free text. +15 tests.
- `6f1d6e8` — `--trace` dispatcher trace viewer (FUTURE 6.1): PII-redacted per-turn FSM/tool/transition table from the existing transcript.
- `bfb3a70` — golden-trace replay (FUTURE 2.3): byte-for-byte ordered FSM-transition + tool-outcome fingerprint regression guard; catches reordering/drops a delta-only check misses.
- `831b99a` — adversarial scenario generator (FUTURE 2.1): composable `PerturbationRule` transforms produce adversarial variants from a base scenario; live-only by design.
- `4a2cda0` — scenario-from-transcript scaffolder (FUTURE 6.2): `scripts/scaffold_scenario.py` turns a `USER:`/`BOT:` transcript into a paste-ready `Scenario` stub with `# TODO` markers for un-inferable fields.

## Phase 10 — Parallel guardian fronts (2026-05-24)

A team-orchestrated pass: ledger reconciliation, then four parallel
single-owner fronts (EHR, conversation, call UI, operator console), each
gated by `make verify` + `make mock-eval` and committed independently.

- `b086e4c` — repo cleanup + per-doc consult/update policy.
- `6c7a6da` — checkpoint the hybrid-llm-navigation WIP (triage, adversarial suite, tester, F6 docs) before multi-agent front work — green baseline captured.
- `59f51fe` — reconcile ledgers with shipped code: mini-LLM specialty router moved in-flight→shipped (SOLUTION §14/§17, ADR 005), FUTURE 1.1/2.1/2.3/6.1/6.2 marked shipped, F-013 added + counts refreshed in ADVERSARIAL_FINDINGS, F7 gains `tester/**` ownership.
- `58bd729` — (F1 EHR) duration bounds `ge=30,le=90` on create/reschedule schemas, xdist-safe test fixtures, seed `name_normalized` via `normalize_name`; +17 tests.
- `2eb4d5f` — (F3 conversation) naturalness pass on per-state task messages; every routing token preserved, all entries <1 KB, mock-eval 56/56.
- `9dcb444` — (F4 call UI) distinct error state, connecting-phase animation, a11y on mute, removed the dead speaker button; WebRTC signaling path unchanged.
- `985b440` — (F5 console) mtime-sorted session picker + connection-state UX; new back-compat `list_sessions_with_meta()`; no bus-shape change.
- `docs` — console event count 8→9 (`turn_interrupted`) in SOLUTION §8 + CLAUDE.md; this Phase 10 entry.

---

## Trajectory at a glance

| Phase | Theme | Tests / scenarios | Tag |
|---|---|---|---|
| 0 | Design + plan | — | `0.0.x` |
| 1 | Vertical slice | first eval suite | `0.1.0` |
| 2 | Perf + reliability | ~130 | `0.1.1` |
| 3 | Security + parallel eval | ~209, 91% cov | `0.1.2` |
| 4 | Offline eval + hygiene | 149 + mock 16/16 | `0.1.3` |
| 5 | Subagent hardening | 155→186 | — |
| 6 | Live-eval truth pass | 186 | — |
| 7 | Id-contract + naturalness | 337, mock 18/18 | — |
| 8 | Eval coverage | mock 18→55 | — |
| 9 | FUTURE roadmap | mock 55/55 | — |
| 10 | Parallel guardian fronts | 564 tests, mock 56/56 | — |

**Recurring discipline across phases:** every change gated by `make verify`
(ruff + format + mypy `--strict` + pytest); `make mock-eval` after any
dispatcher/flows/tools change; root-cause fixes over symptom patches; tools
return `Result[Ok, Err]` with `Err.code` as public eval contract.
