# ADR-002 — Separate FastAPI process + httpx for the EHR

**Status:** Accepted (2026-05-19)
**Deciders:** Matías + LLM council
**Council deliberation:** [`docs/superpowers/specs/2026-05-19-prosper-challenge-design.md`](../superpowers/specs/2026-05-19-prosper-challenge-design.md) §2.3

## Context

The challenge requires an "EHR" with persistent storage and five named operations. We could have embedded the SQLAlchemy session directly into the bot process and called the repository from tool handlers in-process. Instead, the EHR is a standalone FastAPI app the bot talks to via `httpx`. The choice has latency, testability, and realism implications.

## Decision

EHR runs as its own FastAPI process (`src/prosper/ehr/api.py`, mounted on `:8000`). Bot process (`bot.py`, `:7860`) reaches it through `EHRClient` — a thin `httpx.AsyncClient` wrapper with `Keep-Alive` and a single shared client across the call. Eval suite mounts the EHR in-process via `httpx.ASGITransport` (no separate uvicorn during evals — hermetic and ~10× faster than subprocess-based).

## Consequences

- **Positive:** Mirrors how a real EHR integration works (FHIR/HL7 over HTTP). The HTTP contract is the testable boundary — reviewers can `curl` `:8000/docs` to stress-test the EHR independently. Bot and EHR scale and fail independently. The `httpx.ASGITransport` trick gives evals a hermetic in-process EHR with zero network setup.
- **Negative:** ~1 ms HTTP loopback overhead per call. Irrelevant against the LLM round-trip (p50 ~820 ms) — the measured `tool:*` p50 stays under 35 ms. Two processes mean a slightly more involved `make dev` (or `docker-compose up`) story for reviewers.
- **Operational:** Health-check probe at bot startup (`_startup_health_check`) catches "EHR isn't running" before the caller sits through a greeting.

## Alternatives considered

1. **In-process EHR (direct SQLAlchemy session in tool handlers).** Simpler to run, no HTTP overhead. Rejected — loses the realistic integration boundary, makes evals less faithful to production, and couples bot lifecycle to DB lifecycle.
2. **gRPC instead of HTTP.** Tighter contract via protobuf, lower per-call cost. Rejected — overkill for the latency budget, adds tooling burden, no reviewer benefit at this scale.
3. **Shared memory / async queue.** Eliminates serialization. Rejected — doesn't mirror any real EHR pattern and makes the boundary invisible.
