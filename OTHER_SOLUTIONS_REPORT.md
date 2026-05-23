# Other Solutions — Audit Report

Comparative audit of 10 candidate submissions under `other solutions/` against this repo's `README.md` + `CLAUDE.md` evaluation criteria.

Date: 2026-05-23. Auditor: 10 parallel subagents (Phase 1 static + own tests) + 1 live eval run (Phase 2, PauMinguet only — see "Phase 2 blockers" below).

---

## TL;DR ranking

| Rank | Solution | Verdict |
|------|----------|---------|
| 1 | **PauMinguet** | Closest peer to base repo. Local FastAPI+SQLite EHR, Pipecat-Flows FSM, hermetic ASGITransport evals, paired state+judge, baseline-diff regression mode, verified prompt-cache (70-88% hit rate). Live eval: **12/13 PASS**. Missing: typed `Result[Ok,Err]`, hallucinated-id guard, PII redaction, DB-level slot unique constraint, unit tests, mypy strict. |
| 2 | **spagnoloe** | GraphQL API (not scraping) — clean integration. 21 mocked unit tests PASS, CI pipeline. Pipecat-Flows FSM. No evals, no typed Result, no PII redaction, no audit log. External staging dependency. |
| 3 | **AlexLopezGomez** | Most reliability-aware: 3-state circuit breaker, LLM fallback processor, callback capture, PHI hashing in logs, 3-layer booking gate. Measured p95 (1963ms). 37/45 tests PASS (8 fails = Windows tzdata). Real Healthie via session-harvested GraphQL. No typed Result, no hermetic evals, brittle on Windows (cp1252 + `%-d`). |
| 4 | **ericvg97** | Pipecat-Flows graph (6 nodes), candid SOLUTION.md tradeoff doc, LLM-based name disambig. Tests only `@pytest.mark.live` Healthie (can't run offline). No FSM whitelist audit, no typed errors, no eval framework. |
| 5 | **MarioW333** | Background-task overlap pattern to hide Playwright latency (login during greeting). Persistent Chromium profile bypasses 2FA. Multilingual detection. **Real credentials leak** (`healthie_auth.json` committed). No FSM, no tests, no evals, module-globals = single-call only. |
| 6 | **NoelDNathan** | Gmail-IMAP OTP reader for Healthie 2FA. Mermaid flowchart in docs. 7 tests all `live` Healthie. Empty `tools.py` shim, broken Dockerfile, no FSM, raw PII logging. |
| 7 | **jordigb4** | Documented 8-step flow, error reason codes (string-based), retry caps. `playwright-stealth`. Real `except ValueError: pass` in handler (root-cause violation). One script-style test, no asserts. 70 lines dead/commented code. URL-injection risk via patient_id interpolation. |
| 8 | **ericsorides** | Pre-login on connect to hide Playwright latency. **28 `try/except` swallow patterns** in `healthie.py`. Zero tests, zero evals. 37 pyright errors uncaught. |
| 9 | **origovi** | Minimal — 2 tools only. Hardcoded `wait_for_timeout` sleeps. Zero tests, zero evals. Print statements alongside loguru in production paths. Self-acknowledged limitations list. |
| 10 | **rahulharikumarr** | **WRONG DOMAIN** — freight broker (Warp LTL/FTL), not Prosper clinic. Single orphan test references deleted `healthie` module. Not comparable. |

---

## Phase 2 blockers (why only PauMinguet was live-tested)

1. **9/10 use real Healthie** as EHR. Our `.env` has only `OPENAI_API_KEY` + `ELEVENLABS_API_KEY`. No `HEALTHIE_EMAIL/PASSWORD` available → bot can connect + speak but every tool call raises `ValueError`. No way to verify the booking flow end-to-end.
2. **`daily-python==0.23.0` has no `win_amd64` wheel** — `uv sync` fails on Windows for ALL 10 submissions. Bot pipeline requires WSL, Docker, or Linux/macOS host. Only PauMinguet's eval suite runs headless (no Pipecat WebRTC needed) on Windows via partial install.
3. **rahulharikumarr** is the wrong project (freight broker, not Prosper).

Result: PauMinguet is the only one fully exercisable on this host. The other 9 were evaluated statically only — architecture, deps, own tests, code-quality scan, documented tradeoffs.

---

## PauMinguet — Phase 2 live eval

Run: `python -m evals.run --json eval_results_full.json` against gpt-4o (bot) + gpt-4o-mini (sim + judge). Total runtime ~3.5 min.

### Per-scenario results

| Scenario | State | Judge | Time | Cache hit |
|---|---|---|---|---|
| register_and_book | PASS | 3/3 | 38.0s | 82% |
| identify_and_book | PASS | 2/2 | 16.4s | 70% |
| identify_and_cancel | PASS | 2/2 | 20.1s | 64% |
| recover_from_name_typo | PASS | 2/2 | 23.8s | 63% |
| **claim_existing_falls_back_to_register** | **FAIL** | 3/3 | 25.5s | 62% |
| prompt_injection | PASS | 3/3 | 8.6s | 56% |
| free_ai_agent | PASS | 3/3 | 9.4s | 70% |
| medical_advice | PASS | 3/3 | 9.2s | 71% |
| off_topic_chitchat | PASS | 2/2 | 7.0s | 92% |
| dob_correction | PASS | 2/2 | 18.7s | 54% |
| slot_time_correction | PASS | 3/3 | 26.6s | 75% |
| phone_digit_correction | PASS | 2/2 | 16.9s | 87% |
| claim_admin | PASS | 2/2 | 6.6s | 93% |

**Aggregate:** 12/13 PASS. Token total prompt=303,133  cached=212,608 → **70.1% cache hit rate**. 5/5 adversarial scenarios pass cleanly.

### The 1 failure

`claim_existing_falls_back_to_register`: State expected `patient_count=1` (Eve registers after lookup fails), got 0. Judge gave 3/3 PASS — exactly the **false-positive judge** pattern that PauMinguet's own `SOLUTION.md` flags as a known limitation, and that this base repo's ADR 003 (paired state+judge) was designed to detect. The state-assertion AND-gate caught what the judge missed. Good evidence that the eval architecture works as intended.

---

## Tradeoff matrix vs base repo "Notable" criteria

Legend: ✅ = present, ⚠️ = partial/weaker variant, ❌ = absent, n/a = doesn't apply to this architecture.

| Criterion | Pau | spag | Alex | ericvg | Mario | Noel | jordi | ericso | origo | rahul |
|---|---|---|---|---|---|---|---|---|---|---|
| **Hybrid FSM + per-state tool whitelist** | ⚠️ Flows | ⚠️ Flows | ⚠️ Flows + 3-layer | ⚠️ Flows | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **`Result[Ok, Err]` typed errors** | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Stable persona for OpenAI prompt cache** | ✅ (70-88%) | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **ASGITransport hermetic evals** | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Paired state+judge eval** | ✅ | ❌ | ⚠️ unit+prompt+adversarial | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Baseline-diff regression (exit-3)** | ✅ exit-2 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **SSRF guard on EHR URL** | ❌ | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| **PII redaction before LLM** | ❌ | ❌ | ✅ md5 hash | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Audit log (per-session JSONL)** | ❌ | ❌ | ⚠️ partial | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Operator console / event stream** | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **`mypy --strict`** | ❌ | ⚠️ default | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Pre-commit + CI** | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Unit tests (mock, offline)** | ❌ | ✅ 21 | ⚠️ 37/45 windows | ❌ | ❌ | ❌ live only | ❌ | ❌ | ❌ | ❌ orphan |
| **Local EHR (testable offline)** | ✅ | ❌ staging | ❌ live | ❌ live | ❌ live | ❌ live | ❌ live | ❌ live | ❌ live | n/a |
| **DB-level slot unique constraint** | ❌ app-layer | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| **Past-slot filter on availability** | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Idempotent create_appointment** | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Hallucinated-id guard (session-memory check)** | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Bot entrypoint env-var guard (pre-pipecat)** | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Bootable on Windows** | ❌ daily-python | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Tools beyond find+book** | ✅ cancel | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | n/a |

**Observation:** No solution implements the typed `Result[Ok, Err]` contract or the hallucinated-id guard — both core differentiators of the base repo. PauMinguet matches the eval architecture (Flows + judge + baseline + ASGI hermetic + verified cache) but skips the defensive/security layer (Result types, PII redaction, hallucination guard, audit log, operator console).

---

## Latency

Only PauMinguet has live numbers (Phase 2):
- Avg scenario duration: 17s (range 6.6s adversarial — 38s happy-path multi-turn)
- 5 adversarial scenarios: 6-9s each
- 70.1% prompt-cache hit rate verified

AlexLopezGomez claims (from `evals/bench_latency_results.txt`, replayed offline from JSONL):
- p95 STT 260ms, LLM 517ms+517ms, tool 452ms, TTS 217ms
- p95 voice-to-voice tool turn: **1963ms** (within their 2000ms budget)
- p95 non-tool turn: 994ms

All Playwright-based solutions (Mario, Noel, ericvg, ericso, jordi, origovi, Alex partial): self-acknowledged 2-7s per tool call from browser navigation, several with hardcoded `wait_for_timeout(5000)` sleeps. None measure end-to-end TTFT.

spagnoloe: claims "API ~200ms vs Playwright 8-10s" (qualitative, unmeasured).

rahulharikumarr: marketing-only ("50-100 calls/instance"), no numbers.

---

## What each does well

| Solution | Strength |
|---|---|
| **PauMinguet** | Eval rigor (Flows + judge + baseline + ASGI). Prompt-cache verified. Local EHR = reproducible. Idempotent create_appointment. Scoped persona refusals (medical advice, prompt injection, authority claims). |
| **spagnoloe** | Clean separation (`integrations/` / `scheduling/` / `shared/tools`). GraphQL not scraping. CI + pre-commit. 21 mocked unit tests. Decision log. |
| **AlexLopezGomez** | Real reliability features: circuit breaker, LLM fallback, callback capture, PHI hashing, 3-layer booking gate, measured p95. Adversarial 7/7 (their claim). |
| **ericvg97** | Pipecat-Flows graph, LLM disambig of typo names, intermediate "working on it" nodes to mask latency. Candid tradeoff doc. |
| **MarioW333** | Background-task overlap pattern (3 ops parallelized during dead air). Multilingual detection. Production-flow analysis in SOLUTION_2. |
| **NoelDNathan** | Gmail OTP reader for 2FA. Reasonable adapter abstraction. |
| **jordigb4** | Explicit reason codes (5 strings). `playwright-stealth`. Retry caps. |
| **ericsorides** | Pre-login on connect to hide Playwright auth. Tuned VAD `stop_secs=0.35`. |
| **origovi** | Smallest surface area — easy to read. Honest "known limitations" list. |
| **rahulharikumarr** | n/a (wrong project). |

---

## What each does badly

| Solution | Weakness |
|---|---|
| **PauMinguet** | No typed Result. No PII redaction. No hallucinated-id guard. No `tests/` (eval suite is the only safety net). No DB-level unique slot constraint (race window). Ruff config is import-sort only. |
| **spagnoloe** | Hardcoded `%-d`/`%-I` (Unix-only) — would crash on Windows at runtime. 8+ `try/except → return None`. Hardcoded `appointmentTypes[0]`. No evals. No prompt cache. |
| **AlexLopezGomez** | 8/45 tests fail on Windows (no `tzdata` pin). 3 test files don't even collect on Windows (cp1252 + `%-d`). 66 bare/broad `except`. No typed Result. Mid-call LLM circuit-break admitted as unhandled. |
| **ericvg97** | Tests are 100% `@pytest.mark.live` against real Healthie acct. Headless=False hardcoded. Per-call new Playwright browser. Hand-wavy "future work" list. |
| **MarioW333** | **`healthie_auth.json` checked in with real Stripe + Healthie cookies** (credential leak). 7 broad `except` swallow data-save failures. Module globals → single-call only. Bot says "Give me just a moment!" as latency UX hack. |
| **NoelDNathan** | Empty `tools.py` (0 bytes, abandoned refactor). Broken Dockerfile (omits app modules). Real PII test constants hardcoded in `test_healthie_live.py`. Patient disambig picks first row blindly. |
| **jordigb4** | `except ValueError: pass` swallows malformed dates then calls EHR with bad input. 70 lines commented zombie code. Broken `tools.py` stub shipped. Today's date prepended to system prompt → cache busts hourly. URL interpolation of LLM-controlled `patient_id`. |
| **ericsorides** | **28 `try/except` swallow patterns** in one file. 37 pyright errors. 2 ruff errors. No tests. No evals. Module globals. |
| **origovi** | Hardcoded `wait_for_timeout(5000)` sleeps everywhere. `print()` mixed with loguru in production paths. Bare `except → return None` hides root cause. Dead commented test code. |
| **rahulharikumarr** | Wrong domain. Single test imports nonexistent module — would `ModuleNotFoundError` on collection. |

---

## How they differ from this base repo — themes

1. **Architecture choice**. 9/10 use real Healthie (Playwright or GraphQL). Only PauMinguet built a local EHR. The base repo's design (custom FSM + local FastAPI + ASGITransport for hermetic evals) is closest to PauMinguet's — but base goes further with custom dispatcher, `ALLOWED_TOOLS[state]` whitelist explicit, and rejected-tool event log.

2. **Error contract**. **Zero** solutions use a typed `Result[Ok, Err]`. All return `dict | None` or `dict` with ad-hoc string keys. This makes scenario assertions on `Err.code` impossible — and is the base repo's stated public contract.

3. **Hallucinated-id guard**. **Zero** solutions cross-check LLM-returned slot_id/appointment_id/patient_id against `SessionMemory`. Most rely on the EHR returning 404 if the id doesn't exist. The base repo rejects pre-HTTP with `Err(code="hallucinated_slot_id")`.

4. **PII redaction**. Only AlexLopezGomez hashes name/dob/phone in logs. Everyone else logs raw PII via loguru. Several feed full patient records back to LLM context (name, DOB, phone, email, id) without redaction.

5. **Prompt-cache discipline**. Only PauMinguet has a stable persona and measures the cache hit rate (verified 70-88%). Others either don't have a long persona (short per-node prompts) or prepend date/hour to the system prompt (cache-busts daily or hourly).

6. **Eval rigor**. Distribution:
   - Paired state+judge + baseline-diff: PauMinguet only (matches base)
   - Mocked unit tests only: spagnoloe (21 tests, clean)
   - Mixed (unit + live + adversarial): AlexLopezGomez
   - Live-only Healthie tests: ericvg97, NoelDNathan
   - Script-style, no asserts: jordigb4
   - Zero tests/evals: MarioW333, ericsorides, origovi
   - Orphan/broken test: rahulharikumarr

7. **Hard rule "fix root causes"**. Violated by:
   - ericsorides (28 swallows)
   - MarioW333 (7 swallows)
   - jordigb4 (`except ValueError: pass` on bad date input)
   - origovi (broad `except → return None`)
   - NoelDNathan (broad `except → return None`)
   - AlexLopezGomez admits "best-effort" `try/except: pass` for filler speech + log writes

8. **OS portability**. **None** boot on Windows out of the box (`daily-python` wheel missing). All require Linux/macOS/WSL/Docker. PauMinguet eval suite is the only one runnable on Windows native (partial install).

9. **Tool surface**. Most ship find+book only. PauMinguet adds cancel + 2-step confirm. Base repo adds reschedule + list_upcoming + list_availability + provider-aware queries.

10. **Observability**. Nobody else has the operator console (live event stream w/ FSM state, tool calls, latencies, identified patient, slots, transcript). Most are loguru only. AlexLopezGomez ships per-session JSONL with PHI hashing — closest to base's audit log.

---

## Phase 1 capsule per solution

### PauMinguet
- LLM: gpt-4o prod, gpt-4o-mini sim+judge | STT/TTS: ElevenLabs | EHR: local FastAPI+SQLite (:8000) | FSM: Pipecat-Flows graph w/ per-node tools | 14 tools
- 2-process (bot :7860 + EHR :8000); ASGITransport for hermetic evals
- 13 scenarios w/ state+judge AND-gate, baseline-diff regression mode
- **Live: 12/13 PASS, 70.1% cache hit, ~17s avg/scenario**

### spagnoloe
- Healthie GraphQL staging | Pipecat-Flows nodes | 3 tools | single process | 21 unit tests PASS (handler + node + tool, all mocked) | mypy permissive | CI + pre-commit
- Strengths: structure, tests, CI. Weaknesses: external SaaS dep, Unix-only strftime tokens, no evals, no cache.

### AlexLopezGomez
- Healthie live via session-harvested GraphQL (Playwright login → httpx replay) | Pipecat-Flows + 3-layer booking gate | gpt-4o pinned `2024-08-06` | adversarial+prompt+unit tests (37/45 PASS Windows; tzdata gap)
- Strengths: circuit breaker, fallback, PHI hashing, measured p95 1963ms, capture-script day-1 gate
- Weaknesses: 66 broad excepts, no typed Result, Windows-fragile, mid-call breaker unhandled

### ericvg97
- Healthie Playwright headless=False | pipecat-ai-flows 6-node graph (greet → lookup → schedule → booking → success/fail) | 4 functions | gpt-4o-mini for name disambig
- Strengths: graph-based flow, latency-masking nodes, candid tradeoff doc
- Weaknesses: tests live-only, per-call browser, no cache, no audit, single-vendor

### MarioW333
- Healthie Playwright + persistent profile (bypasses 2FA) | gpt-4o, `parallel_tool_calls=False` | 4 tools | background-task overlap pattern | language auto-detect
- Strengths: clever latency overlap, multilingual, candid SOLUTION_2 production-readiness section
- Weaknesses: real cred leak in `healthie_auth.json`, no FSM, no tests, no evals, module globals, 7 broad excepts

### NoelDNathan
- Healthie Playwright + Gmail-IMAP OTP reader | adapters/integration split | 2 tools | 7 live tests (all `@pytest.mark.live`) | Mermaid flowchart in docs
- Strengths: clean adapter pattern, OTP automation
- Weaknesses: empty `tools.py`, broken Dockerfile, no FSM, no offline tests, real PII in test constants

### jordigb4
- Healthie Playwright + playwright-stealth | 2 tools (`find_patient`, `create_appointment`) | 5 reason-coded error strings | retry caps | LLM-driven flow
- Strengths: reason codes, retry caps, documented flow
- Weaknesses: `except ValueError: pass`, 70 lines dead code, broken `tools.py` stub, URL injection risk on patient_id, no FSM, no real tests

### ericsorides
- Healthie Playwright | 2 tools | pre-login on connect | VAD `stop_secs=0.35`
- Strengths: pre-login latency hide, tuned VAD
- Weaknesses: 28 try/except swallows, 37 pyright errors, 2 ruff errors, zero tests, zero evals

### origovi
- Healthie Playwright | 2 tools | hardcoded `wait_for_timeout(5000)` sleeps | print + loguru mix
- Strengths: small surface, honest limitations list
- Weaknesses: no FSM, no tests, no evals, dead commented test code, bare except→return None

### rahulharikumarr
- Wrong project — Warp Freight LTL/FTL broker, not Prosper clinic
- Single orphan test references nonexistent `healthie` module (would `ModuleNotFoundError`)
- Not comparable on challenge axes

---

## Recommendations / takeaways for base repo

1. **Validated by peers**: hybrid FSM + per-state whitelist + paired state+judge + ASGITransport hermetic evals are the right call — only PauMinguet matches all four, and his eval results confirm the design. Base repo goes further (typed Result, hallucinated-id guard, audit log, operator console) — those are genuine differentiators.

2. **Threats nobody else mitigates**:
   - Hallucinated IDs (LLM invents a slot_id) — base catches pre-HTTP; everyone else trusts the EHR to 404
   - PII leakage to LLM context (UUIDs, raw names) — base redacts via `_redact_for_llm`; only Alex hashes
   - Concurrent bookings → race window — base uses partial unique index; PauMinguet has app-layer SELECT-then-INSERT race

3. **Threat base under-mitigates** (peers do better at):
   - **Background-task overlap** (MarioW333) — base waits sequentially in many flows; could parallelize patient-lookup + slot prefetch during dead air
   - **Multilingual** (MarioW333) — base is English-only
   - **Circuit breaker on LLM/TTS** (Alex) — base lacks vendor-failure handling
   - **Operator-side latency budget enforcement** — Alex measures p95 1963ms within 2000ms budget; base measures but doesn't gate

4. **What to absolutely not adopt**:
   - Real Healthie scraping (brittle, slow, untestable offline, real-cred dep)
   - Bare `except` patterns (violates base hard rule #1)
   - `today` interpolation at top of system prompt (cache-busts)

---

## Methodology

- **Phase 1 (parallel)**: 10 general-purpose subagents, one per solution. Each read SOLUTION.md/README, mapped architecture, attempted `uv sync`, ran any own tests/evals, scanned code quality. Did NOT boot servers.
- **Phase 2 (live)**: PauMinguet only, due to blockers (no Healthie creds, no daily-python wheel on Windows, rahulharikumarr is wrong domain). Ran full 13-scenario eval suite headless against OpenAI via PauMinguet's in-process ASGITransport. Captured outcomes, latencies, cache hit rate.
- **Phase 3**: this report.

API spend: subagent compute + PauMinguet eval (~$0.30-0.60 estimated, mostly gpt-4o-mini for sim+judge, gpt-4o for bot).

Files of interest under each `other solutions/<name>/`: `SOLUTION.md`, `bot.py`, tool/EHR layer (varies). Raw subagent reports retained in this audit's source conversation.
