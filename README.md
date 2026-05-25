# Prosper Health Challenge — voice agent + EHR

Voice agent that books and cancels appointments at a fictional clinic.
Built on Pipecat + ElevenLabs STT/TTS + OpenAI LLM, backed by a
self-built FastAPI EHR (SQLite + SQLAlchemy).

Non-technical overview in [`SOLUTION.md`](./SOLUTION.md); the full engineering
reference + operations manual (section-by-section design + decision trail) in
[`ARCHITECTURE.md`](./ARCHITECTURE.md).

## Prerequisites
- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- API keys for ElevenLabs and OpenAI
- `make` (Linux/macOS). On Windows, use the **uv-direct commands** column
  in the table below — they are exactly what the `make` targets run.

## Setup
```bash
cp env.example .env       # add ELEVENLABS_API_KEY and OPENAI_API_KEY
make install              # or: uv sync
make seed                 # or: uv run python scripts/seed.py
make mock-eval            # zero-cost: 16 scenarios via deterministic mock LLM in ~5 s — verifies the eval suite works without burning a single token
```

## Run
Two processes (one terminal each):
```bash
make ehr                  # http://localhost:8000  (FastAPI EHR + Swagger /docs)
make bot                  # http://localhost:7860  (Pipecat browser client)
```
Open `http://localhost:7860`, click **Connect**, talk to the agent.

While the bot is running, the **Operator Console** is also live at
[`http://localhost:7861/console`](http://localhost:7861/console). It
shows the FSM state, every tool call with its outcome and latency, the
identified patient (PII redacted), slots offered, the live transcript,
and the final outcome banner — split into a clinical pane (top) and a
dev pane (bottom). Set `PROSPER_CONSOLE_ENABLED=0` to disable, or
override the port with `PROSPER_CONSOLE_PORT=...`.

Every call also writes an **append-only audit log** under `data/audit/`
— one `<session_id>.jsonl` per call. Useful one-liners:

```bash
ls data/audit/                                              # list sessions
jq '.type' data/audit/<session_id>.jsonl                    # one event type per line
jq 'select(.type=="outcome")' data/audit/<session_id>.jsonl # final outcome only
jq 'select(.type=="tool_call_end") | {tool: .payload.tool, code: .payload.code, ms: .payload.duration_ms}' \
  data/audit/<session_id>.jsonl                             # tool timeline
```

All PII fields are masked at the event boundary (see ADR 004), so the
JSONL is safe to share for audit / replay without further redaction.

Or with Docker: `docker-compose up`.

## All commands

| `make` target    | uv-direct equivalent (works on Windows)                                          | Purpose                                                          |
|------------------|----------------------------------------------------------------------------------|------------------------------------------------------------------|
| `install`        | `uv sync`                                                                        | Install deps from `uv.lock`                                      |
| `seed`           | `uv run python scripts/seed.py`                                                  | Populate SQLite (3 providers, 14 days of slots, 2 demo patients) |
| `ehr`            | `uv run uvicorn prosper.ehr.api:app --host 0.0.0.0 --port 8000 --reload`         | FastAPI EHR + Swagger `/docs` on `:8000`                         |
| `bot`            | `uv run bot.py`                                                                  | Pipecat browser client on `:7860`                                |
| `test`           | `uv run pytest tests/ -v`                                                        | 149 unit tests + 17 skipped (no external services); 91% line coverage on `src/prosper` (97% excluding `bot.py`) |
| `mock-eval`      | `uv run python -m evals --mock-llm`                                              | Run all 16 scenarios offline with the deterministic mock LLM (~5 s, no API key) |
| `eval`           | `uv run pytest evals/test_scripted.py -v`                                        | Live scripted scenarios (needs `PROSPER_EVAL_LIVE=1` **and** `OPENAI_API_KEY`) |
| `audio-smoke`    | `PROSPER_AUDIO_LIVE=1 uv run pytest evals/audio_smoke -v`                         | Real acoustic TTS→STT round-trip via ElevenLabs (needs `ELEVENLABS_API_KEY` + `PROSPER_AUDIO_LIVE=1`; spends credits, never runs in `verify`) |
| `eval-baseline`  | `uv run python -m evals --json evals/results/current.json --baseline evals/results/baseline.json` | CLI eval with regression diff against a snapshot. Supports `--concurrency N` (default 4) for parallel runs |
| `verify`         | `uv run ruff check ...` → `ruff format --check` → `mypy --strict` → `pytest -q`  | One-shot pre-submit gate: lint + format + type + tests, stops on first failure |
| `lint`           | `uv run ruff check src/ tests/ evals/` then `uv run ruff format --check ...`     | Ruff lint + format check (`I,E,F,W,B,UP,ARG,SIM,RET,RUF,S`)      |
| `type`           | `uv run mypy src/prosper`                                                        | `mypy --strict` on the package                                   |
| `bench`          | `uv run python scripts/bench.py --rounds 10`                                     | EHR endpoint micro-bench (needs `make ehr` running on `:8000`)   |
| `status`         | `uv run python scripts/status.py`                                                | Repo health snapshot: test count, coverage, dirty files, lint status |
| `pre-commit`     | `uv run pre-commit run --all-files`                                              | Run the full pre-commit suite                                    |
| `clean`          | `rm -rf data/ .pytest_cache/ .mypy_cache/ .ruff_cache/ evals/results/`           | Reset local caches and DB                                        |

Running `tests/` plus the two eval unit-test modules (`evals/test_runner_checks.py`
+ `evals/test_types.py`) collects **149 passing + 17 skipped** tests
total; `evals/test_scripted.py` (live OpenAI) is gated behind
`PROSPER_EVAL_LIVE=1` + `OPENAI_API_KEY`, while `test_scenario_mock`
(parametrised over all 16 scenarios via `evals/mock_llm.py`) runs on
every push.

### Eval CLI exit codes (`python -m evals` and `make eval-baseline`)

| Code | Meaning                                                                          |
|------|----------------------------------------------------------------------------------|
| `0`  | All selected scenarios passed (state + judge).                                   |
| `1`  | At least one scenario failed.                                                    |
| `2`  | Bad invocation (no `OPENAI_API_KEY` set, or no scenarios matched the filter).    |
| `3`  | `--baseline` regression: a scenario that previously passed now fails.            |

## Environment variables (`env.example`)

| Var                          | Default                       | Purpose                                                                                  |
|------------------------------|-------------------------------|------------------------------------------------------------------------------------------|
| `ELEVENLABS_API_KEY`         | _required_                    | ElevenLabs STT (Realtime) + TTS (Flash v2.5).                                            |
| `OPENAI_API_KEY`             | _required for bot + eval_     | OpenAI Chat Completions for the dispatcher LLM and the eval judge.                       |
| `PROSPER_EHR_URL`            | `http://127.0.0.1:8000`       | Where the bot reaches the EHR. SSRF-guarded: scheme must be `http`/`https` and the URL must have a hostname. |
| `PROSPER_DB_URL`             | `sqlite:///data/ehr.db`       | SQLAlchemy DSN. Swap to Postgres without code changes.                                   |
| `PROSPER_BOT_MODEL`          | `gpt-4o-mini`                 | Primary LLM for the dispatcher.                                                          |
| `PROSPER_BOT_FALLBACK_MODEL` | _unset_ (e.g. `gpt-4o`)       | Optional secondary model — tried once if the primary exhausts its retry budget.          |
| `PROSPER_EVAL_MODEL`         | `gpt-4o-mini`                 | Model used by the eval judge (`evals/judge.py`) and persona simulator (`evals/sim.py`). Live evals reuse `PROSPER_BOT_MODEL` for the dispatcher, mirroring `bot.py`. |
| `PROSPER_AUDIO_LIVE`         | _unset_ (set to `1` to run)   | Opt-in gate for the acoustic audio-smoke suite (`make audio-smoke`). Must be `1` **and** `ELEVENLABS_API_KEY` set, or the suite skips — so a bare `pytest` never spends ElevenLabs credits. |
| `PROSPER_BOT_ENTRYPOINT`     | _unset_ (set to `1` in prod)  | When `1`, missing required env vars `SystemExit(2)` *before* the 17 s pipecat import wall instead of crashing mid-call. Tests deliberately leave it unset so imports don't blow up. |
| `PROSPER_CONSOLE_ENABLED`    | `1`                           | Operator Console on/off. Set to `0` to skip the second uvicorn (useful for tests or headless CI). |
| `PROSPER_CONSOLE_PORT`       | `7861`                        | TCP port for the Operator Console. One above the Pipecat browser client (`7860`) — adjacent and easy to remember. |
| `PROSPER_CONSOLE_HOST`       | `127.0.0.1`                   | Bind address. Override to `0.0.0.0` behind a reverse proxy. |
| `PROSPER_CONSOLE_AUDIT_ROOT` | `data/audit`                  | Directory holding the per-session JSONL audit files. |
| `PROSPER_CONSOLE_HEARTBEAT_S`| `15.0`                        | SSE keep-alive interval. Lower for laptop demos, higher if proxies drop on idle. |
| `PROSPER_CONSOLE_REPLAY_SPEED`| `1.0`                        | Replay pacing. `1.0` = real-time, `0.0` = as-fast-as-possible. |
| `PROSPER_CONSOLE_REPLAY_MAX_GAP_S`| `5.0`                    | Cap on any single replay pause — even a 10-minute idle in the original call won't stall the replay. |
| `PROSPER_CONSOLE_QUEUE_DEPTH`| `256`                         | Per-subscriber bus queue depth. Sized for a 60-s connection hiccup at the observed publish rate. |

## Project layout
```
src/prosper/                   # bot, dispatcher, flows, prompts, tools, llm, ehr_client
src/prosper/ehr/               # FastAPI EHR (models, repository, api, schemas, db w/ WAL)
src/prosper/observability/     # TimingCollector + redact_pii / mask_name
evals/                         # Scenario dataclasses, runner, judge, persona sim, CLI
evals/mock_llm.py              # deterministic mock LLM (~700 LOC) powering `make mock-eval`
tests/                         # 149 unit tests + 17 skipped (EHR + dispatcher + tools + llm + redact + mock scenarios)
docs/adr/                      # 3 Architecture Decision Records (001 hybrid FSM, 002 separate EHR process, 003 paired state+judge)
docs/architecture.md           # ASCII process + FSM diagrams
docs/bench-results.md          # pinned EHR-bench snapshots (pre/post each perf wave)
docs/glossary.md               # terminology cheat-sheet (FSM, eval, Pipecat, OpenAI vocabulary)
docs/interview-notes.md        # candidate prep — also doubles as decision evidence
docs/research/                 # audit / security / reliability / perf research notes
docs/superpowers/              # design specs + implementation plan
scripts/seed.py                # one-shot DB seeding
scripts/bench.py               # re-runnable EHR-endpoint micro-bench (`make bench`)
scripts/status.py              # repo health snapshot (`make status`)
CONTRIBUTING.md                # how to add scenarios / tools / states
SECURITY.md                    # SSRF guard, PII redaction, threat model
CHANGELOG.md                   # reverse-chronological delivery log
.editorconfig / .gitattributes # consistent line endings + indent across editors
```

## Notable
- Hybrid FSM with per-state tool whitelist enforced at the dispatcher level
- `Result[Ok, Err]` typed tool returns; `Err.code` is the public eval contract
- Pre-seeded `Slot` rows make idempotency a unique-constraint, not a lock
- Paired state-assertion + LLM-judge in every scenario eval
- Long stable `CLINIC_PERSONA` (~1400 tokens) for OpenAI prompt-cache hits
- `httpx.ASGITransport` mounts the EHR in-process during evals — hermetic + fast

## Endpoints (FastAPI EHR)
```
POST  /patients
GET   /patients/by-phone?phone=...
GET   /patients/by-name-dob?name=...&dob=YYYY-MM-DD
GET   /patients/{id}/appointments
GET   /availability?date=YYYY-MM-DD[&provider_id=...]
POST  /appointments                        # body: {patient_id, slot_id, notes?}
POST  /appointments/{id}/cancel            # body: {reason?}
GET   /health
```
Live `OpenAPI` schema at `http://localhost:8000/docs` once `make ehr` is running.
