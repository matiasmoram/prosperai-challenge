# src/prosper/console/ — Front F5: Operator console + event bus

Live-monitoring dashboard fed by the dispatcher over a fire-and-forget event bus,
served on `:7861`. Owner front: **F5**. Ownership map: `../../../FRONTS.md`.
Design: `../../../docs/superpowers/specs/2026-05-20-operator-console-design.md`
+ `../../../docs/adr/004-operator-console-event-stream.md`.

> **Sub-front below me:** `static/call/` → **Front F4** (WebRTC caller UI). It has
> its own CLAUDE.md and is **static assets only**. You (F5) own the Python + the
> serving plumbing; F4 owns the call page's html/js/css.

Parallel-safe with F1 F2 F3. Shares seam **S2** with F4 (serving plumbing — see below).

## What lives here (F5-owned)
- `events.py`  — the **8 typed events**: `state_change`, `transcript_turn`,
  `tool_call_start`, `tool_call_end`, `latency_tick`, `patient_identified`,
  `slots_offered`, `outcome`. + `validate_event` (rejects unmasked `*_masked` fields)
- `bus.py`     — `ConsoleBus`: bounded async queues, **overflow-drop** semantics
- `sse.py`     — SSE stream + `mount_static_on_app` (mounts BOTH `/console/static` and `/call/static`)
- `server.py`  — console uvicorn app (`:7861`, override `PROSPER_CONSOLE_PORT`)
- `audit.py`   — `AuditJSONLWriter`: non-blocking append to `data/audit/<session>.jsonl`
- `_utils.py`  — `mask_name` / `mask_phone` helpers
- `../../../tests/console/**` — your test suite (this front's gate)

## Contracts you MUST NOT break
- **Telemetry never breaks the call path.** Every publish site is fire-and-forget,
  wrapped in try/except; bounded queues drop on overflow so a slow SSE client can
  never back-pressure a live call. A bug here must degrade telemetry, never the call.
- **PII never reaches the bus raw.** `_redact_tool_args` + `mask_name`/`mask_phone`
  run at the event boundary; `validate_event` rejects any `*_masked` payload that
  looks unredacted. The audit log redacts on the write path (F-007 fix). The live SSE
  showing raw transcript text to the operator is intentional; the durable log is not.
- **`outcome` categories are load-bearing.** `booked` / `cancelled` / `refused`
  (offer declined) / `abandoned` (dropped pre-confirm) are distinct. Collapsing
  `refused`↔`abandoned` poisons clinic dashboards.
- **`tester/` (F7) rides these event shapes.** `tester/receipt_gate.py` reads the
  bus stream — `outcome` as a *claim*, `tool_call_end.outcome == "ok"` as a *receipt*.
  Renaming an outcome category or changing the `tool_call_end` payload shape can break
  the gate. Run `make tester` after any event-shape change.
- **Adding a dispatcher tool/state means deciding which events fire** and threading
  `_redact_tool_args` — but that publish wiring lives in `dispatcher.py` (**F2**),
  coordinate; you own the event *shape* here.

## Seam S2 — serving plumbing (you own it, F4 lives behind it)
`sse.py::mount_static_on_app` mounts both `/console/static` (F5) and `/call/static` (F4).
If F4 needs a **new mount path or route**, that edit to `server.py`/`sse.py` is a
**F5 change** — an F4 agent must not touch this Python. Coordinate the route, then F4 fills the assets.

## Verify gate
```
uv run pytest tests/console -v     # your slice
make verify                        # full gate before ANY commit
```

## Open work — derive fresh each cycle (manager's job)
- `../../../docs/testing/ADVERSARIAL_FINDINGS.md` — **F-007 (audit PII)** and
  **F-010 (cross-session bus eviction)** are CLOSED (per-session subscribe + audit-write
  redaction shipped). Confirm via their tests before re-opening; don't re-fix.
- `../../../ARCHITECTURE.md` §8 — current console wiring; update it in the same change if you alter event shapes (root rule 11).
