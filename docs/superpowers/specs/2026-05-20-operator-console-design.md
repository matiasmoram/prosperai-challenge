# Operator Console — Design Spec

**Status:** Approved — implementation in progress.
**Author:** Matías Quirós · 2026-05-20
**Tracking task list:** in-session TaskList #1–10.

---

## 1. Why this exists

Prosper interviewers signalled two things in the scope Q&A:

1. **Stick with the cascading Pipecat + ElevenLabs STT/TTS + OpenAI LLM stack.** Speech-to-speech (`gpt-realtime-2`) is "easier, more boring, and usually works worse." → No pivot to S2S.
2. The default Pipecat browser client is *not* interesting on its own ("aquesta part de la UI no és especialment interessant"), **but** they hinted at "alguna cosa relacionada amb el challenge que si pot ser guay tenir una vista ;)". → A clinic-side view of the active call is on-scope and *expected* to differentiate the submission.

The first point is a stack constraint. The second is the brief for this spec.

The Operator Console is the answer: a real-time, split-pane web view that shows what is happening inside the running voice agent — for the clinic operator during a live call, and for the interviewer during the demo. It is **not** rebranding and it is **not** decoration. It is the product surface a clinic would actually use.

### Why this matters strategically

A voice agent for clinics is not a chatbot. It is a clinical workflow tool, and clinical workflow tools require operator visibility for three reasons:

- **Safety / training:** A human supervisor needs to monitor automated calls during the early rollout phase. They need to *see* what the bot decided and why.
- **Audit / HIPAA-adjacent compliance:** Every appointment booked, cancelled, or refused must be reconstructible after the fact. "Who did what to whom, when, and why" is auditor table-stakes.
- **Debugging at runtime:** A tool call failing with `slot_taken` is not a stack trace — it is a clinical event that the operator should see *in plain language*, not buried in JSON logs.

Building the console therefore demonstrates three things at once: product thinking (clinical UX), engineering rigor (event sourcing, SSE, replay), and compliance-aware architecture (audit log on disk by default).

---

## 2. Goals & non-goals

### Goals

1. Live, split-pane view of the active call, embedded in the **same Python process** as the bot — no extra terminal. The console binds its own uvicorn on `:7861` (one above Pipecat's `:7860` browser client) because `pipecat-ai-cli` does not expose a hook to mount additional routes on its FastAPI app. See ADR 004 § "Secondary decisions worth recording".
2. Two simultaneous languages: clinical-product top half, technical-internals bottom half. Same event stream, two renderers.
3. Per-session JSONL audit log on disk, written append-only by a non-blocking subscriber. Survives process restart.
4. Replay any past session by id — same UI, deterministic playback from the JSONL.
5. Zero-cost backwards compatibility: if the console module is not wired in, the dispatcher behaves exactly as before. All existing 179 tests + 16 mock-eval scenarios stay green.
6. Backend additions are type-strict (`mypy --strict`), ruff-clean, and unit-tested. No `try/except: pass`.

### Non-goals

- **No WebSocket / bidirectional channel.** Operator does not interrupt the call from the UI yet. SSE is enough and avoids the WS-on-Pipecat complexity. (Listed in FUTURE.md if we ever need it.)
- **No SQLite-backed audit.** JSONL is enough: append-only, one file per session, grep-friendly, replay-friendly. SQLite is overkill for the demo and adds a migration story we do not need.
- **No React, no build step, no bundler.** One `index.html` + one `console.js` + Tailwind via CDN. Lower diff-surface, faster cold open, smaller dep tree.
- **No new env-var coupling for the bot.** The console is opt-in via `PROSPER_CONSOLE_ENABLED=1` (defaults on for dev, leave off in tests). Existing env contracts unchanged.

---

## 3. Architecture

### 3.1 New module: `src/prosper/console/`

```
src/prosper/console/
├── __init__.py
├── events.py          # ConsoleEvent dataclasses (typed payloads)
├── bus.py             # async pub/sub, asyncio.Queue under the hood
├── audit.py           # JSONL writer subscriber + replay loader
├── sse.py             # FastAPI router: /console/stream, /sessions, /replay
└── static/
    ├── index.html     # Split-pane shell, Tailwind CDN
    └── console.js     # EventSource client, DOM mutators
```

Self-contained. The only existing-code change required is `dispatcher.py` adding ~12 lines to publish events, gated on `if self._bus is not None`.

### 3.2 Event flow

```
Dispatcher.handle_user_turn ──> ConsoleBus.publish(event) ──┬──> SSE subscriber ──> EventSource (browser)
                                                            │
                                                            └──> AuditJSONLWriter ──> data/audit/{session_id}.jsonl
                                                                                       (also readable by Replay)

Replay: client requests /console/replay/{id}
        ──> AuditJSONLWriter.iter_events(id) ──> SSE stream (same format as live)
```

Both consumers see the *same* event format. Replay is a pure stream-of-recorded-events — the front-end does not branch on "live vs replay", it just consumes SSE.

### 3.3 Event types (8)

Each event is a frozen dataclass with `type: Literal[...]`, `ts: float`, `session_id: str`, `payload: dict[str, Any]`.

| Type | When published | Payload shape (key fields) |
|---|---|---|
| `state_change` | `Dispatcher` transitions FSM | `from: str, to: str, trigger: str` |
| `tool_call_start` | Before handler dispatch | `tool: str, args_redacted: dict, call_id: str` |
| `tool_call_end` | After handler returns / errors | `tool: str, call_id: str, outcome: "ok"\|"err", code: str\|None, duration_ms: float` |
| `patient_identified` | `SessionMemory.identified_patient` set | `name_masked: str, dob_year: int, phone_masked: str, id_internal: str` |
| `slots_offered` | `list_availability_slots` returns ≥1 slot | `count: int, first_date: str, last_date: str, providers: list[str]` |
| `transcript_turn` | After user text + bot text per turn | `role: "user"\|"bot", text: str, turn_id: int` |
| `outcome` | Reaching `END` | `outcome: "booked"\|"cancelled"\|"refused"\|"abandoned", details: dict` |
| `latency_tick` | Every `TimingCollector.span` close | `phase: str, duration_ms: float` |

**PII redaction at the event boundary.** Patient payloads pass through the existing `observability/redact.py` helpers (`mask_name`, `redact_pii`) *before* publication. The JSONL file therefore contains redacted PII by default — the raw values never leave the dispatcher process unredacted.

### 3.4 Backpressure / blocking story

Publishing is a non-blocking `asyncio.Queue.put_nowait()` per subscriber. If a subscriber's queue is full (e.g. a slow browser), the **subscriber's** queue drops oldest events; the dispatcher path never blocks and never raises. Per CLAUDE.md hard rule #1 ("fix root causes, no `try/except: pass`"), the drop is explicit and logged via `logger.warning` once per session, not silently swallowed.

### 3.5 Threading model

Everything is asyncio-native — there is no threading, no shared mutable state outside the bus. The audit writer runs as one `asyncio.create_task(drain_loop())` per session, owning its own `aiofiles` handle. The SSE endpoint hands the request its own subscriber queue and removes it on disconnect. Cleanup is enforced by `async with bus.subscribe() as q:` (context-managed subscription, guarantees removal).

---

## 4. Data flow walkthrough — happy-path booking

1. Caller dials in. Pipecat connects. `Dispatcher` instantiates with `bus = console_bus` (injected from `bot.py` on startup).
2. Dispatcher publishes `state_change(from=None, to=GREETING)`.
3. Caller speaks. `transcript_turn(role=user, text="Hi, I'd like to book for Sarah Mendez.")`.
4. Dispatcher publishes `state_change(GREETING → IDENTIFY_PATIENT)`.
5. `tool_call_start(find_patient_by_phone, args_redacted={phone: "+1***0142"})`.
6. EHR returns hit. `tool_call_end(outcome=ok, duration_ms=42)`.
7. `patient_identified(name_masked="S**** M.****", dob_year=1988, phone_masked="+1***0142", id_internal="<uuid>")`. UUID is *internal* to the event payload, redacted before it ever reaches the LLM.
8. Caller asks for availability. `state_change → BOOK_FLOW`.
9. `tool_call_start(list_availability_slots, ...)` → `tool_call_end(outcome=ok)` → `slots_offered(count=2, providers=["Patel", "Chen"])`.
10. Caller picks slot. `state_change → CONFIRM_BOOK`.
11. `tool_call_start(create_appointment, ...)` → `tool_call_end(outcome=ok)`.
12. `state_change → END`. `outcome(outcome="booked", details={provider: "Patel", time: "Tue 10am"})`.

Throughout, `latency_tick` events fire on every span close. The dev panel renders the running p50/p95 from those ticks.

---

## 5. Frontend design

### 5.1 Layout

Split pane, vertical. Top 60% = clinical, bottom 40% = dev. Resizable horizontal divider (CSS-only, no JS for the resize).

**Top (clinical) — read like a recepcionista screen:**
- Patient card (masked name + DOB year + masked phone + identification badge).
- Activity strip: a single bold sentence describing what the bot is doing right now in **human language** ("Booking · picking a time", "Cancelling Tuesday's appointment", "Confirming details").
- Slot list: chronological cards as they are offered (date, time, provider lastname).
- Transcript: most recent 6 turns, scrolling.
- Outcome banner: appears at `outcome` event, color-coded (emerald booked, amber cancelled, slate refused, neutral abandoned).

**Bottom (dev) — looks like Chrome devtools:**
- FSM badge with the raw state name (`BOOK_FLOW`, `CONFIRM_BOOK`).
- Tool timeline: row per call, columns = tool name, args summary, outcome chip, duration ms.
- Live latency strip: `llm p50 / tool p50 / ttft / cache_hit_rate` updating every second.
- Toggle button to expand a raw event log (line-per-event JSON, hidden by default).

### 5.2 Palette

Healthcare-clean, no gradients, no glass. Tailwind tokens:

- Background clinical: `slate-50` with `white` cards
- Background dev: `zinc-900` with `zinc-800` cards, `zinc-300` text
- Primary accent: `sky-600` (links, active state)
- Success: `emerald-500` (booked, ok)
- Warning: `amber-500` (slot_taken, retries)
- Refusal/neutral: `slate-500`
- Error: `rose-600` (uncaught exception, never shown on happy path)

Typography: `Inter` via CDN for headings, system mono for the dev panel.

### 5.3 Three pages

| Route | Purpose |
|---|---|
| `GET /console` | Picks the most recent open session and redirects to `/console/{id}`. |
| `GET /console/sessions` | Sidebar list of recent sessions with start time + outcome + duration. |
| `GET /console/{id}` | Live view for an open session, or replay for a closed one. The page does not need to know which — it requests `/console/stream/{id}` and the backend decides whether to stream live or replay-from-JSONL. |

---

## 6. Backend interfaces

### 6.1 `events.py`

```python
@dataclass(frozen=True, slots=True)
class ConsoleEvent:
    type: str           # one of EVENT_TYPES literal
    ts: float           # time.time(), seconds
    session_id: str
    payload: dict[str, Any]
```

A single dataclass keeps serialisation trivial. Per-type schemas live in docstrings + the `events.py` `validate(event)` helper that raises on malformed payloads (defence in depth — bus.publish calls validate).

### 6.2 `bus.py`

```python
class ConsoleBus:
    async def publish(self, event: ConsoleEvent) -> None: ...
    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[ConsoleEvent]]: ...
```

In-memory only. One process. Fan-out N subscribers, each with its own bounded queue (`maxsize=256`). On overflow, drops oldest with a single `logger.warning("subscriber overflow ...")`.

### 6.3 `audit.py`

```python
class AuditJSONLWriter:
    def __init__(self, root: Path = DEFAULT_AUDIT_ROOT) -> None: ...
    async def attach(self, bus: ConsoleBus) -> AsyncIterator[None]: ...
    async def iter_events(self, session_id: str) -> AsyncIterator[ConsoleEvent]: ...
```

- One file per session at `data/audit/{session_id}.jsonl`. One JSON object per line.
- Writes via `aiofiles` with `fsync` on `outcome` (the only durability-critical event).
- Replay reads the file, yields events with `ts` rewritten to monotonically increasing wall-clock so the front-end can render at real speed (a small `await asyncio.sleep(...)` per gap).

### 6.4 `sse.py`

Three endpoints:

| Method + path | Behaviour |
|---|---|
| `GET /console/stream/{session_id}` | If session is live (in-memory in bus), tail it via SSE. If closed, replay from JSONL. SSE format: `data: {json}\n\n`. Heartbeat `: ping\n\n` every 15 s. |
| `GET /console/sessions` | JSON `{sessions: [{id, started_at, ended_at, outcome, duration_ms}]}`. Reads `data/audit/` index. |
| `GET /console/replay/{session_id}` | Explicit replay endpoint. Same SSE format as live. Useful when you want replay even of a still-open session (for QA). |

### 6.5 Dispatcher integration

The `Dispatcher` constructor gains an optional `bus: ConsoleBus | None = None`. Every existing call site that *doesn't* care (tests, eval runner, mock dispatcher) continues to pass nothing. The bot wires it up:

```python
# bot.py
console_bus = ConsoleBus()
audit_writer = AuditJSONLWriter()
# attach as background task
asyncio.create_task(audit_writer.attach(console_bus))

dispatcher = Dispatcher(..., bus=console_bus)
```

Inside the dispatcher, ~12 lines are added at the existing observability hook points. **No new control flow** — pure pass-through publishing.

---

## 7. Testing strategy

| Layer | Test | Tool |
|---|---|---|
| `events.py` | Round-trip serialisation, validate rejects bad payloads | pytest |
| `bus.py` | Publish before subscribe, subscribe before publish, fan-out N subscribers, overflow behaviour | pytest + asyncio |
| `audit.py` | Write event → file exists → read back identical, replay yields events in order, two writers on same session id raise (one-writer invariant) | pytest + tmp_path |
| `sse.py` | SSE smoke via `httpx.ASGITransport` — start a stream, publish 3 events, assert client receives 3 frames in order. Replay endpoint reads JSONL fixture and emits matching frames. | pytest-asyncio + httpx |
| Dispatcher integration | Run existing scenario with bus attached; assert bus subscribers see the expected event sequence. No regression on existing 179 tests. | pytest |
| PII redaction | Mutation test: bus.publish never emits unredacted name/phone/DOB | pytest + property-style |
| Frontend | Manual browser smoke during the dev-server run; document in README with a screenshot | manual |

All Python tests live in `tests/console/` (new directory). Target: keep `src/prosper` coverage ≥ 91 %.

---

## 8. Documentation deliverables

1. This spec (you are reading it).
2. **ADR 004** — `docs/adr/004-operator-console-event-stream.md` — captures the three load-bearing decisions: SSE not WS, JSONL not SQL, redact at event boundary not at storage.
3. **README** new section "Operator Console" with screenshot + how to open `/console`.
4. **`docs/glossary.md`** — `ConsoleEvent`, `ConsoleBus`, replay terminology.
5. **`FUTURE.md`** — mark item 5.1 "Immutable Tool-Call Audit Log" as done; demote to "polish" (structured logging, redaction unit tests in CI).

---

## 9. Decision log (justify-every-step appendix)

The reviewer is expected to ask "why this, not that?" for each load-bearing choice. The answers, on paper, ahead of time:

- **Why SSE, not WebSocket?** SSE is one-way (server → browser). We do not need duplex for v1 (no operator-initiated interruption). SSE rides plain HTTP, no protocol upgrade, works through corporate proxies, auto-reconnects in the browser. WS would buy us duplex we cannot use yet and a heavier failure mode.
- **Why JSONL, not SQLite/Postgres?** One file per session = append-only by construction. Grep-able for incident response. No migration story to maintain. Cost of replay = O(filesize). For demo + initial production, this is enough; the abstraction (`AuditJSONLWriter`) leaves a clean swap path if we outgrow it (see ADR 004 § "When to swap").
- **Why publish-and-forget at the dispatcher boundary?** The dispatcher's job is the FSM, not telemetry. If a subscriber is slow, the call must still complete. Backpressure is the *subscriber's* problem; we drop on overflow and log it. Voice-agent latency is precious — no synchronous telemetry calls on the hot path.
- **Why redact at the event boundary, not at storage?** Defense in depth. If the audit file is leaked, the data is already masked. If a future subscriber (e.g. ship-to-Slack) is added, it cannot accidentally leak raw PII — the bus has none.
- **Why split-pane, not two pages?** During the demo the interviewer wants to see *both* layers without alt-tabbing. The split-pane single page is the only layout that lets the audience watch the clinical narrative and the engineering substrate update in lockstep.
- **Why same `:7860` process, not a separate service?** A separate console process needs its own startup, port, deployment story. The marginal cost of one FastAPI router on the existing app is trivial; the marginal cost of two processes is non-trivial. We already have a `bot.py` server with FastAPI — we mount on it.
- **Why no React?** Build step adds ~1 day. Vanilla JS + Tailwind CDN renders the same visual in ~250 LOC with hot reload via browser refresh. The view is read-only; we have no form state, no router state, no client cache. React buys nothing here.
- **Why event types as a closed list of 8, not free-form?** A closed list is testable, replayable, and survives schema migrations (a missing type = old log, a new type = forward-compat consumer). Free-form event payloads would defeat both the JSONL audit story and the typed front-end renderers.

---

## 10. Implementation order (tracked in TaskList #1–10)

1. Spec doc (this file) — commit.
2. `events.py` + `bus.py` — unit-tested in isolation.
3. `audit.py` — JSONL round-trip tested.
4. `sse.py` — SSE smoke via httpx ASGI.
5. Dispatcher integration — 12-line diff, regression-checked.
6. Frontend `index.html` + `console.js` — manual browser sanity.
7. Wire-up in `bot.py` — single dev-server smoke.
8. ADR 004 + README + glossary + FUTURE update.
9. Tests (`tests/console/`) consolidated.
10. Final verify gate: ruff + format + mypy strict + pytest + mock-eval + manual browser open.

Each step is followed by a subagent reviewer pass (per user goal): the reviewer checks the diff for hardcoded values, missing docstrings, missing tests, justification gaps, and CLAUDE.md compliance. Findings come back as a one-line punch list; the next step does not start until the punch list is empty.
