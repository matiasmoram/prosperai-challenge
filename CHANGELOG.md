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
- `b16021a` — spec v2; refined design and scope.
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

## Phase 11 — Feature waves: triage duration, intent UX, F6 mail+calendar, reliability (2026-05-24/25)

Team-orchestrated wave program: an LLM-council decided the duration rule, an
auditor caught generalization hazards, a continuous adversarial-gen loop grew the
mock suite, and the F6 mail+calendar+handoff feature shipped. mock-eval 56 → 105.

- `9910e49` — EHR query-param max_length caps (by-phone/by-name-dob).
- `1c63b3a` — **duration soft-override** (LLM-council): caller may extend freely / go
  shorter than recommended after one nudge; triage gains minimum_safe_minutes + rationale.
- `8728199` — EHR `GET /appointments` clinic-calendar read (F6 Task 1).
- `06b01da` — context-aware CHOOSE_INTENT: no cancel/reschedule offer with 0 upcoming appts
  (one ~10ms prefetch on entry).
- `579a630`,`33796df` — tester hardening: reject generated personas with unfilled
  placeholders + flag corrupt sim runs.
- `d0fe150` — richer seed: 2 providers/specialty, varied patients, weekday-only slots.
- `f149c80` — **clinical-floor guard** (audit F-002): below-minimum_safe_minutes booking →
  Err(below_minimum_safe_duration), a code contract not a prompt wish. +unit tests.
- `df954b4`,`c2c12f3`,`e284367`,`291db01` — continuous adversarial/contradiction scenarios
  + fuzzy-band + deterministic past-slots test (mock-eval → 105).
- `e4e2b57`,`c60b3a0` — **F6 Mail + Calendar**: full-PII MailStore, `/frontdesk` router+SPA,
  `leave_message_for_front_desk` tool + `HANDOFF` state + `handed_off` outcome + stuck-detector
  safety-net + booking-confirmation mail, wired live. ADR 006.
- `ff962dc` — identity-not-found → front-desk handoff (req 1): insists-existing-but-unmatched
  → leave message, never duplicate-register.
- `740880c` — LLM-total-failure path (req 4): canned line + reception mail, never dead air.
- `18cfde1` — barge-in: unit-cover interruption truncation; infra was already wired (§14 was stale).
- `ad802d4` — doctor choice when a specialty has 2+ providers (prompt-only).
- `01d2d0b` — route_intent reschedule-vs-cancel disambiguation at the schema root + pinpoint scenarios.

### Phase 12 — review mode (reviewer teams + anti-hardcoding, root-cause)

- `8d6e1c0` — **floor-guard string bypass** (review): `isinstance(int)` failed open on an
  LLM string/float `duration_minutes`; coerce like pydantic so sub-floor bookings are rejected.
  +observability (silent floor-clamp + prefetch error now logged) + two-tier-routing doc.
- `4093cbf` — mail file-path session-id sanitisation (path-traversal defense-in-depth, PII store).
- `f764a21` — **messy-human sim, deterministic core**: `tester/noise.py` (seeded disfluency +
  ASR-error injectors) + `tester/clarification.py` (the `plowed_ahead_on_garble` contract).
- `f8eae67` — messy-human sim, live arm: `Persona.noise_profile` garbles caller turns, simulator
  audits the bot re-prompted; 4 MESSY personas; folded into the violation tally + exit code.
- `5185dfd` — **cancel + reschedule front-desk mail**: lifecycle parity with `booking_confirmation`.
  `_emit_cancellation_notice` (provider/start recovered from `last_upcoming_appointments`) +
  `_emit_reschedule_notice`; shared `_caller_identity()`/`_fire_mail()`; SPA rose/blue kinds;
  +4 tests (`test_mail_lifecycle.py`). 5 mail kinds total.
- `3e8309a` — **SOLUTION.md accuracy pass**: fix stale counts (scenarios 107/63→108, handlers
  8→9, events 8→9, ADRs 001..004→001..006) + restore file-map omissions (`integrations/`,
  `observers.py`, `speculation.py`, `run_all.py`/`frontdesk_server.py`/`sim_call.py`, `tester/`).
- `7a285e3` — **AvailabilityCache adjudicated spec** (FUTURE 1.3, council-decided): full 4-tuple
  key, repo-layer placement, evict-on-commit (reschedule=2 dates, multi-slot=all chained dates),
  TTL 10s default-off, DB-409 stays the guard. Documented, not built (remote-EHR-only payoff).
- `27bf511` — **specialty wording normalised to canonical EHR value (from live log)**: caller chose
  "Dermatology", bot looped "no slots … next six days" forever though Dermatologist was wide open.
  Root (data/contract, not the LLM): `providers.specialty` stores "Dermatologist"/"Therapist"/… and
  the EHR filters case-insensitive EXACT, but the caller-facing menu says "Dermatology"/"Therapy"/… —
  the LLM passes the menu word, matches nothing, 4 of 5 specialties silently unbookable. mock-eval was
  green because scenarios pass the canonical value. Fix: `tools._normalize_specialty` (difflib fuzzy
  map to SPECIALTY_DURATION_TABLE keys, cutoff 0.6) at the tool boundary, before primary + scan probes;
  unknown specialties pass through. +unit test. ARCHITECTURE §13.3. 769 tests, mock-eval 108/108.
- `877670a` — **scrub references to other candidates' solutions (submission hygiene)**: the repo
  cited other candidates' private submissions by name across docs (some with file-line code
  citations). Deleted the pure-competitor-analysis spec, anonymized the prior-art discussion in
  `interruption_design.md` + `speculative_race.md` (design takeaways kept, names/paths dropped),
  removed name-drops elsewhere, and renamed the already-gitignored local reference folder to a
  neutral name (`.gitignore`/`.dockerignore` updated). `git grep` for every name/path/phrase now
  returns zero; the reference material stays local-only and unpushable. Docs only, no code change.
- `00e3731` — **doc test-count reconciliation**: SOLUTION.md (×2) + ARCHITECTURE §0 still said
  ~715 tests (`tests/` 544 + `tester/` 171) while the verify set now collects 770 (`tests/` 582 +
  `tester/` 172 + evals 16; 769 pass / 1 skip). CHANGELOG already recorded 769 at `27bf511` — only
  the two reference docs lagged, which a reviewer running the suite would catch. Doc counts only,
  no code change. ruff + format + mypy --strict clean, 769 pass, mock-eval 108/108.
- `e8f23de` — **CONFIRM_BOOK lists specialties, never refuses (prompt, from live log — Phase 1)**:
  caller in CONFIRM_BOOK asked "what type of doctors are there?" and the bot refused (in-scope
  question). Root (design smell, ARCHITECTURE §14): `BOOK_FLOW→CONFIRM_BOOK` fires on *slots listed*,
  not *slot selected*, so the question landed in a confirm-a-slot prompt. Phase 1 (council A→B,
  prompt-only, zero FSM/eval blast radius): CONFIRM_BOOK now names the five specialties + re-lists on
  a "what do you offer / something different?" turn. Phase 2 (transition fires on real selection) is
  committed in the same ticket, tracked in §14. test_prompts 10/10 (1009 B < 1 KB), mock-eval 108/108.
- `db34ef3` — **consent gate (architecture, from live log)**: completes the offer-then-confirm
  invariant. `_READ_BEFORE_WRITE` only blocked list+book in the SAME turn; a CONFIRM_* state
  persists across turns, so the model could fire create/cancel/reschedule on a turn that was a
  QUESTION (live `7c9d55a5`: caller asked "tell me which ones are not taken" → model booked an
  un-offered Ben Osei slot, ended the call, then hallucinated "no slots"). New gate refuses the
  write when the caller's last turn matches `_INFO_SEEKING` and NOT `_AFFIRM` — asymmetric, so
  natural picks ("the 2:30", "go ahead") still commit; zero extra LLM round-trips; doesn't touch
  `create_patient`. +2 tests. ARCHITECTURE §13.7 + §14 (offered-slot redesign as follow-up).
- `d93d39d` — **confirm states keep their read tool (architecture)**: CONFIRM_BOOK/CANCEL/RESCHEDULE
  no longer expose only the write — they regain `list_availability_slots`/`get_upcoming_appointments`
  so a non-yes ("which are free?") re-offers instead of cornering the bot into booking a slot the
  caller never chose (live bug). Prompts: commit on natural confirmation, never on a question. +test.
- `4ba58e2` — **barge-in VAD + diagnostics**: live, the bot never stopped on interrupt (0
  `turn_interrupted`). Pipecat's interruption logic is correct; the VAD wasn't detecting the barge-in
  over the bot's audio. VAD tuned harder (`confidence` 0.35→0.25, `min_volume` 0.15→0.06) + per-call
  `VAD: user started speaking`/`INTERRUPTION fired` logs to localise detection-vs-flush vs mic/echo.
- `9a07885` — **date + filler fixes (prompt half, from live log)**: dated 10-day weekday table in
  the `[CONTEXT]` anchor (LLM was resolving 'next Monday' to a Sunday date); neutral fillers (CONFIRM_*/
  REGISTER no longer say 'Booking that now' while the caller is declining).
- `11ec5bc` — **offer-then-confirm gate + afternoon slots (architecture half, from live log)**:
  `_READ_BEFORE_WRITE` blocks a write in the same turn its read ran (model had booked an unconfirmed
  morning slot on 'afternoon any day' — a real 201); slot summary now shows a day-spanning spread, not
  just the earliest 6 (afternoon was invisible). +`tests/test_offer_before_commit.py`. ARCHITECTURE §7.
- `f769584` — **barge-in stress suite** (`tests/test_barge_in_stress.py`, 26 cases): interrupt at
  every point of a reply, 10 consecutive interrupted turns, rapid repeats, spurious bursts, interrupt
  +choppy barge-in (words not lost), interrupt+silence. + `47c78fc` docs: SOLUTION (plain) +
  ARCHITECTURE §14 (technical) explanation of how interruption handling works.
- `fed4919` — **acoustic audio smoke** (`evals/audio_smoke/test_audio_smoke.py`): real ElevenLabs
  TTS→STT round-trip (bot's voice + `eleven_flash_v2_5` → `scribe_v1`) — intent survives, time-of-day
  not flipped, bot reply intelligible. Live-verified green. Double-gated (`ELEVENLABS_API_KEY` +
  `PROSPER_AUDIO_LIVE=1`), `make audio-smoke`, never in `verify`. Closes the last partial deliverable.
- `c09d1ba` — **offline barge-in/cutoff pipeline test** (`test_barge_in_pipeline.py`): wires
  `DispatcherProcessor` + `TTSAudibleObserver` with injected frames — barge-in truncates the real
  history, spurious interrupt doesn't clobber, hang-up cancels pending aggregation, barge-in words
  not dropped. Closes the staging-only gap; $0, in `make verify`. Acoustic loop stays deferred (2.4).
- `478db64` — **doc split**: `SOLUTION.md` → non-technical executive overview;
  technical reference + ops manual `git mv`'d to **`ARCHITECTURE.md`** (history kept,
  §-numbers unchanged). Rule 11 ledger now = `ARCHITECTURE.md`; all active `SOLUTION.md §N`
  pointers repointed (CLAUDE×4, CONTRIBUTING, README, FUTURE, FEATURES, tester/README, code comments).

Open items for the human: `OPEN_QUESTIONS.md` (barge-in live verification, FUTURE 4.1
cross-call provider memory, two-tier CHOOSE_INTENT routing authority).

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
| 11 | Feature waves (triage/UX/F6/reliability) | 685 tests, mock 105/0 | — |

**Recurring discipline across phases:** every change gated by `make verify`
(ruff + format + mypy `--strict` + pytest); `make mock-eval` after any
dispatcher/flows/tools change; root-cause fixes over symptom patches; tools
return `Result[Ok, Err]` with `Err.code` as public eval contract.
