# ADR 004 · Operator Console as an Event-Sourced Side Surface

**Status:** Accepted · 2026-05-20
**Context window:** Prosper Health voice-agent challenge, post-MVP polish wave.
**Supersedes / depends on:** ADR 001 (FSM dispatcher), ADR 002 (separate EHR), ADR 003 (paired state+judge).

## Context

The default Pipecat browser client at `:7860` lets a caller talk to the
bot. It shows them nothing about what is happening inside the agent —
which is fine for the caller but useless for two real audiences:

1. **The clinic operator during early rollout.** Before a voice agent is
   trusted to book appointments unattended, a clinical staff member
   shadows the calls. They need to see, in plain language, what the bot
   decided and on what evidence.
2. **The interviewer during the demo.** A reviewer of this submission
   needs to grasp the FSM rigour, the tool gating, the latency budget —
   without reading source or stepping through a debugger.

We need a UI surface that talks to both audiences from the same data.

We also need it to be **demo-grade fast to build**: the candidate has
~1–2 days for this wave, not weeks.

## Decision

Build a read-only "Operator Console" as a separate FastAPI app, served
by an **embedded uvicorn on `:7861`** inside the same Python process as
the voice bot. The console consumes a typed event stream
(`ConsoleEvent`) emitted by the dispatcher at 8 well-defined hook
points, fanned out by an in-memory pub/sub bus to:

- a Server-Sent Events HTTP endpoint (`/console/stream/{session_id}`)
  for live operator viewing,
- a per-session JSONL audit log on disk (`data/audit/<id>.jsonl`) for
  durable replay and post-call analysis.

The browser front-end is a single static HTML file plus one vanilla-JS
controller (~300 LOC) served by the same uvicorn at `/console/static/`.

Three irreversible architectural choices justify their own ADR space:

### 1. SSE, not WebSocket

The operator console is one-way (server → browser). SSE rides plain
HTTP, auto-reconnects on disconnect by default in `EventSource`, works
through corporate proxies that block WebSocket upgrades, and adds zero
new dependencies (FastAPI's `StreamingResponse` is enough). WebSocket
would buy duplex we cannot use yet — the operator does not interrupt
the call from the UI in v1.

Cost of being wrong: if operator-initiated barge-in becomes a
requirement, we add a second WS endpoint without touching the SSE one.
Migration cost is bounded.

### 2. JSONL on disk, not SQL

One file per session, append-only, one JSON line per event. The schema
is the `ConsoleEvent` dataclass; the canonical serialiser is
`ConsoleEvent.to_json()`.

Why not SQLite or Postgres:

- Replay is O(file-size) sequential read, which is cheap and naturally
  matches the SSE consumption pattern.
- Append-only-by-construction means we cannot accidentally rewrite or
  re-order audit events — a desirable property for an audit log.
- `grep` and `jq` Just Work for incident investigation.
- No migration story, no schema versioning, no DB lock contention.
- The `AuditJSONLWriter` abstraction has one swap point if we outgrow
  it: today the SSE replay endpoint reads from `iter_events`; tomorrow
  that method could front a SQL query without changing the front-end.

When to swap to SQL:

- When concurrent operator queries across thousands of sessions become
  routine (filtering, aggregation, joins with clinic data).
- When per-row access control (which clinician sees which call) is
  needed.

Neither holds for the challenge.

### 3. Redact at the event boundary, not at storage

Every `*_masked` payload field is masked **before** it reaches the
bus. `make_event` + `validate_event` enforce this with a defence-in-
depth check that rejects events whose `_masked` field looks
unredacted (no mask char, or carries a 7+ digit run).

Why at the boundary:

- The audit JSONL inherits the redaction for free — even if a leaked
  file ends up on a developer laptop, it has no raw PHI in it.
- A future subscriber added to the bus (Slack notifier, OTLP exporter,
  external dashboard) cannot accidentally leak raw PII either — there
  is no raw PII on the bus to leak.
- Redaction at storage would protect *the file* but not *the wire*. We
  protect both.

### Secondary decisions worth recording

- **Embedded uvicorn on `:7861`, not Pipecat's `:7860`.** `pipecat-ai-cli`
  does not expose a hook to mount additional FastAPI routes on its
  internal app. Spinning a second uvicorn in the same event loop is
  cheaper than forking pipecat and clearer than monkey-patching it.
- **Opt-in by `PROSPER_CONSOLE_ENABLED=1`.** Default *on* in dev,
  trivially flipped off for unit tests / eval CI runs that should not
  bind a port.
- **No React, no build step.** One HTML + one JS file kept under 350
  LOC together, served by uvicorn's `StaticFiles`. Adds ~30 KB of CDN
  Tailwind to the browser cost; zero impact on Python deploy.
- **Closed list of 8 event types.** `EventType = Literal[...]` is the
  single source of truth; `EVENT_TYPES` is derived via `get_args` so
  the two cannot drift. Adding a 9th type means editing the Literal
  AND `_REQUIRED_KEYS` AND the front-end `dispatch()` switch — three
  fixed places, easy to audit.

## Consequences

### Positive

- **Reviewer narrative.** The interviewer sees the FSM transitions,
  tool gating, and latency budget in real time while the bot answers
  the call — no need to step through code.
- **Operator narrative.** The clinic can plug an operator in front of
  the screen during early rollout. Production-thinking signal without
  shipping a full ops product.
- **Audit-ready.** The on-disk JSONL is the audit log a clinical
  compliance reviewer would actually ask for. The redaction story is
  evidence-based, not just a doc claim.
- **Eval support.** Any past session can be replayed via
  `/console/replay/{id}` for QA and post-mortems.
- **Zero blast radius on the voice path.** Publishing is fire-and-
  forget through bounded queues. A slow or disconnected subscriber
  cannot block the dispatcher.

  The overflow direction is **drop oldest, keep newest**. The most
  valuable signal for a live operator is the most recent state — in
  particular the `outcome` event at END. Drop-newest would mean a
  subscriber that briefly lagged would never see the outcome of the
  call, the single most important event. The trade-off is a small
  hole in the middle of the JSONL (if a subscriber and the audit
  writer both overflow; in practice only the SSE subscriber overflows,
  and the JSONL is written by its own writer with its own queue).

### Negative

- **Two ports to remember in the demo.** `:7860` for the caller's
  browser, `:7861` for the operator. Mitigated by README + a single
  paragraph in the demo notes.
- **No cross-process live view.** The bus is in-memory; the operator
  must connect to the SAME process running the dispatcher. (Replay
  works cross-process via the JSONL.)
- **Heartbeat-bounded disconnect cleanup.** A dropped SSE client's
  subscription stays in the bus until the next heartbeat tick (max 15
  s). Acceptable here; flagged in §5 of the design spec.
- **Tailwind via CDN.** Adds a third-party fetch on console load. The
  browser cache makes this a one-time cost; the trade-off vs.
  introducing a JS build is firmly in favour of the CDN.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Mount on Pipecat's internal FastAPI app | No public hook; would require forking pipecat-ai-cli. |
| Mount on the EHR FastAPI (`:8000`) | The EHR runs in a different process; the bus is in-memory, no live view across processes. |
| WebSocket bidirectional channel | Duplex unused in v1; SSE simpler and proxy-friendlier. |
| SQLite-backed audit | Migration + schema versioning cost; concurrency model overkill for one-file-per-session. |
| React + Vite + Tailwind | ~1 day of build setup; the view is read-only and small enough that 250 LOC of vanilla JS is clearer. |
| Speech-to-speech (gpt-realtime-2) | Interviewer explicitly told us *not* to pivot to S2S ("more boring, usually worse"). Out of scope. |
| Branding-only rebranding (logo + colours, no functional change) | Adds no engineering signal. Interviewer wink (";)") suggested a clinical view, not chrome. |

## Open questions

1. **Multi-operator broadcast.** The current bus fans out to N
   subscribers but every operator sees every session. Per-operator
   filtering by clinic / role is left to a future ADR if needed.
2. **Live tail of JSONL across processes.** If we ever need a process
   to consume events from a *different* process's bus, the cleanest
   path is JSONL polling with a sentinel file watcher (Linux: inotify,
   Windows: ReadDirectoryChangesW). Not required for the challenge.

## Related

- Spec: `docs/superpowers/specs/2026-05-20-operator-console-design.md`
- Code: `src/prosper/console/{events,bus,audit,sse,server}.py`
- Tests: `tests/console/*.py`
- Frontend: `src/prosper/console/static/{index.html, console.js}`
