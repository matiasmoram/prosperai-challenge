# Prosper Health Challenge — voice agent + EHR

Voice agent that books and cancels appointments at a fictional clinic.
Built on Pipecat + ElevenLabs STT/TTS + OpenAI LLM, backed by a
self-built FastAPI EHR (SQLite + SQLAlchemy).

Full design + decision trail in [`SOLUTION.md`](./SOLUTION.md).

## Prerequisites
- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- API keys for ElevenLabs and OpenAI

## Setup
```bash
cp env.example .env       # add ELEVENLABS_API_KEY and OPENAI_API_KEY
make install              # uv sync
make seed                 # populate SQLite with providers + slots + demo patients
```

## Run
Two processes (one terminal each):
```bash
make ehr                  # http://localhost:8000  (FastAPI EHR + Swagger /docs)
make bot                  # http://localhost:7860  (Pipecat browser client)
```
Open `http://localhost:7860`, click **Connect**, talk to the agent.

Or with Docker: `docker-compose up`.

## Test
```bash
make test                 # unit tests (no external services)
make eval                 # scripted scenario evals (needs OPENAI_API_KEY)
make lint                 # ruff lint + format check
```

## Project layout
```
src/prosper/             # bot, dispatcher, flows, prompts, tools, llm, ehr_client
src/prosper/ehr/         # FastAPI EHR (models, repository, api, schemas, db)
src/prosper/observability/   # TimingCollector
evals/                   # Scenario dataclasses, runner, judge, persona sim, CLI
tests/                   # unit tests (EHR + dispatcher + tool handlers)
docs/superpowers/        # design specs + implementation plan
scripts/seed.py          # one-shot DB seeding
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
