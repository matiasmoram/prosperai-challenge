# src/prosper/ehr/ — Front F1: EHR backend (data + API)

The **source of truth** for the whole system: FastAPI + SQLAlchemy on SQLite.
The bot reads/writes here over HTTP (`EHRClient`); tests + evals mount it
in-process via `httpx.ASGITransport` (no socket). Owner front: **F1**.
Ownership map + parallel-safety matrix: `../../../FRONTS.md`.

Parallel-safe with F2 F3 F4 F5 — zero shared files. Not on any seam.

## What lives here
- `api.py`        — FastAPI routes (full endpoint↔challenge map in `ARCHITECTURE.md` §9)
- `db.py`         — engine/session; auto-migrates `Provider.specialty` on startup
- `models.py`     — SQLAlchemy `Patient` `Provider` `Slot` `Appointment` + partial idx
- `repository.py` — persistence; single LEFT-OUTER-JOIN availability (was N+1, 115ms→~10ms)
- `schemas.py`    — Pydantic request/response; `Field(max_length=…)` DoS caps
- `../../../scripts/seed.py` — seeds `data/ehr.db` (NY-local → UTC → strip tzinfo)
- `../../../tests/ehr/**`    — your test suite (this front's gate)

## Contracts you MUST NOT break (each rots the system silently, no error)
- **`slot.start_at` is naive UTC.** Seed strips tzinfo. Any code touching
  `start_at` follows the same convention — mixing naive-*local* with naive-UTC
  misreads availability with no exception raised.
- **One active appointment per slot = partial unique index** on
  `Appointment(slot_id) WHERE status='scheduled'`. Concurrent booking races
  surface as **409 `slot_taken`, never 500**. 409 is terminal, not retryable.
- **Past slots filtered server-side** in `list_availability_slots`. Keep it —
  it's why the bot can't offer 8am at 11am.
- **Every string schema field keeps a `Field(max_length=…)`.** Removing one
  reopens the multi-MB-POST → SQLite DoS.
- **`Err.code` strings are public eval contract.** `evals/scenarios.py` asserts
  on them. Renaming a code is a breaking change → update scenarios same commit
  (this also pulls in F7, and the bot-side mapping in `tools.py` = F2 seam S1).

## Example — adding a column / changing the schema
```
1. edit models.py            3. make seed
2. delete data/ehr.db        4. uv run pytest tests/ehr -v
```
Auto-seed fires only on an **empty** DB — you must delete the file, not just re-run seed.

## Don't touch
Anything outside `ehr/` + `scripts/seed.py` + `tests/ehr/`. The tool handlers
(`tools.py`), FSM (`flows.py`), and dispatcher are **Front F2**. A new `Err.code`
the bot consumes is an F2 coordination (seam S1) — flag it, don't reach across.

## Verify gate
```
uv run pytest tests/ehr -v     # your slice, fast
make verify                    # full gate before ANY commit: ruff + format + mypy --strict + pytest
```

## Open work — derive fresh, don't trust a frozen list (manager's job)
Live ledgers, read these each cycle:
- `../../../FUTURE.md` §1.3 AvailabilityCache (60s TTL, `repository.py`), §5.2 input-validation
  hardening (E.164 `PhoneStr`, DOB bounds, strip HTML from notes — `schemas.py`).
- `../../../docs/testing/ADVERSARIAL_FINDINGS.md` — **F-001…F-013 are CLOSED.** F-005
  (self-collision mislabel, `repository.py`) was fixed; re-confirm via its test before
  re-opening. Do not author fixes for already-fixed findings.
- `../../../ARCHITECTURE.md` §9 — current EHR shape; update it in the same change if you alter it (root rule 11).
