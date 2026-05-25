# FRONTS.md — work-front map for parallel agents

Purpose: let several agents work the repo at once **without colliding on
files**. A "front" is a bounded slice of the codebase with a clear owner set
of files. Two fronts are *parallel-safe* only when they share **no files** and
have **no logical dependency** (the orchestration rule in `CLAUDE.md`).

Read this before dispatching an agent. Assign each agent exactly one front and
paste its **owned files** list as the `files:` scope.

> This is an ownership map, not a physical reorg. No files were moved — imports,
> `mypy --strict`, the Makefile, and the 300+ tests are untouched. The fronts
> are logical lanes over the existing layout.

---

## The fronts

| # | Front | One line | Owned files |
|---|---|---|---|
| **F1** | **EHR backend** (data + API) | FastAPI service, DB, persistence — *backend part 1* | `src/prosper/ehr/**` (`api`, `db`, `models`, `repository`, `schemas`), `scripts/seed.py`, `tests/ehr/**` |
| **F2** | **Agent core** (orchestration) | FSM engine, tool registry, LLM adapter, EHR client — *backend part 2* | `src/prosper/dispatcher.py`, `flows.py`, `tools.py`, `llm.py`, `ehr_client.py`, `result.py`, `speculation.py`, `observers.py`, `observability/**`, `bot.py` (root) + `src/prosper/bot.py` |
| **F3** | **Conversation design** | How the bot *talks* / what it *asks* — persona, copy, triage prompts | `src/prosper/prompts.py` |
| **F4** | **Call frontend** | WebRTC caller UI (the phone-call page) | `src/prosper/console/static/call/**` (`index.html`, `call.js`, `call.css`) |
| **F5** | **Operator console** | Live-monitoring dashboard + event bus | `src/prosper/console/{server,bus,sse,events,audit,_utils}.py`, `console/static/{console.js,index.html}`, `tests/console/**` |
| **F6** | **Mail + Calendar** *(BUILT — Wave 4, 2026-05-25)* | Staff handoff inbox + booking-confirmation mail + EHR calendar | `src/prosper/integrations/mail.py`, `integrations/router.py`, `integrations/static/**`; dispatcher mail wiring (`dispatcher.py`); console wiring (`console/server.py`, `bot.py`); `docs/adr/006-handoff-state.md` |
| **F7** | **Evals / QA** | Scenario + test suite + hallucination harness — cross-cutting | `evals/**`, `tests/**` (except `tests/ehr`, `tests/console`), `tester/**`, `docs/testing/**` |

### Serving topology (so F4/F5 don't confuse ports)

- **`:7860`** — Pipecat runner (`bot.py` → `prosper.bot`). Serves WebRTC signaling at `POST /api/offer`. Owned by **F2**.
- **`:7861`** — console uvicorn (`console/server.py`). Serves **both** `/console` (operator dashboard, **F5**) and `/call` (custom call UI static, **F4**). Override via `PROSPER_CONSOLE_PORT`.
- The call page (F4) is **static assets only**; its `call.js` dials `:7860/api/offer` directly (`BOT_ORIGIN`). It does **not** run its own backend.

---

## Shared seams (the only places fronts touch)

Everything not listed here is single-owner. These three surfaces need a rule.

### S1 — The spine: `tools.py` + `flows.py`  *(owner: F2)*

`tools.py` holds `TOOL_SCHEMAS` (line ~447) + `HANDLERS` (line ~723) registries.
`flows.py` holds `State` enum, `ALLOWED_TOOLS[state]` (line ~31), `TRANSITIONS` (line ~49).

Adding a tool (F6) or a new state / question (F3) **must** edit these. Rule:

- **Only one agent edits `tools.py`/`flows.py` at a time.** If F3 and F6 both need spine edits, sequence them or route both through the F2 agent.
- A spine change is a **public-contract change** (`Err.code`, `ALLOWED_TOOLS`) → it requires an eval scenario in `evals/scenarios.py` + `make mock-eval` (hard rule 4). That makes it an **F7-coupled** change too.

### S2 — Console serving plumbing: `console/server.py` + `console/sse.py`  *(owner: F5)*

`sse.py::mount_static_on_app` mounts **both** `/console/static` and `/call/static`.
F4 (call frontend) lives behind this but should only edit assets under
`static/call/**`. Rule:

- F4 edits **static files only**. If F4 needs a **new mount path or route**, that edit to `server.py`/`sse.py` is an **F5 change** — coordinate, don't let an F4 agent touch F5 Python.

### S3 — Pipeline build: `src/prosper/bot.py`  *(owner: F2)*

The Pipecat pipeline + `:7860` signaling. F4 does **not** touch it (it signals
in from the browser). F6's booking hook lands in `dispatcher.py` (F2), **not**
`bot.py`. Rule: only F2 edits `bot.py`.

---

## Parallel-safety matrix

✅ = run simultaneously, different files · ⚠️ = shared seam, coordinate/sequence · — = self

| | F1 | F2 | F3 | F4 | F5 | F6 |
|---|---|---|---|---|---|---|
| **F1** EHR | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| **F2** core | ✅ | — | ⚠️ S1 | ✅ | ✅ | ⚠️ S1/S3 |
| **F3** convo | ✅ | ⚠️ S1¹ | — | ✅ | ✅ | ⚠️ S1¹ |
| **F4** call FE | ✅ | ✅ | ✅ | — | ⚠️ S2 | ✅ |
| **F5** console | ✅ | ✅ | ✅ | ⚠️ S2 | — | ✅ |
| **F6** mail/cal | ✅ | ⚠️ S1/S3 | ⚠️ S1¹ | ✅ | ✅ | — |

¹ F3 is **✅ with everyone** as long as it edits **only `prompts.py`** (pure
copy/persona). It becomes ⚠️ S1 only when "what to ask" means **adding a
state/transition** (`flows.py`).

**F7 (evals/tests) is not a parallel lane.** It validates whatever a front
lands and edits `tests/**` broadly. Run it as the gate *after* a front merges,
not concurrently with the front that changes the same behavior.

**Safest concurrent batch today:** F1 + F2 + F4 + F5 (four agents, zero shared
files). Add F3 only if it stays in `prompts.py`.

---

## §F6 — Mail + Calendar (SHIPPED Wave 4, 2026-05-25)

### What was built

```
src/prosper/integrations/
  __init__.py
  mail.py       # MailStore (JSONL append), MailMessage dataclass, make_message()
  router.py     # /frontdesk FastAPI router + SPA (full-PII staff tier)
  static/       # front-desk SPA assets (served at /frontdesk/static)
```

Spine additions (F2 seam — one-time, additive):
- `flows.py`: `State.HANDOFF`, `INTERNAL_TOOLS` updated, `leave_message_for_front_desk`
  whitelisted in CHOOSE_INTENT / BOOK_FLOW / CANCEL_FLOW / RESCHEDULE_FLOW, `needs_human`
  transition edges, `handed_off → END` + `goodbye → END` from HANDOFF.
- `tools.py`: `LEAVE_MESSAGE_TOOL` constant + `leave_message_for_front_desk` entry in
  `TOOL_SCHEMAS`; absent from `HANDLERS` (dispatcher-intercepted).
- `dispatcher.py`: `mail_store: MailStore | None` param; `_handle_leave_message`;
  `_emit_booking_confirmation`; `_emit_safety_net_handoff`; `_outcome_published` dedup;
  `handed_off` outcome; HANDOFF treated same as END for tool-loop termination.
- `console/server.py`: `build_app` + `run` gain `store` + `calendar_fetch` params;
  `/frontdesk` router conditionally included.
- `bot.py`: constructs `MailStore` + async `calendar_fetch`; passes both to `Dispatcher`
  and `build_frontdesk_router` under the console-enabled gate.

### Design properties (as shipped)

- **Fire-and-forget, never breaks the call path.** `_inflight_publishes` strong-ref set
  keeps tasks alive; every write wrapped in try/except; failure logged, not surfaced.
- **No added latency.** `asyncio.create_task` schedules the write off the turn; the LLM
  never waits for SMTP/JSONL.
- **Identity never from LLM args.** `_handle_leave_message` reads `SessionMemory`
  exclusively — the LLM supplies only `category`, `summary`, `callback_wanted`.
- **`/frontdesk` is loopback-only in demo.** Must be behind auth in production. See
  `SECURITY.md` + `docs/adr/006-handoff-state.md`.

### Eval coverage

- `caller_requests_human` (tag: handoff, f6) — happy-path `leave_message_for_front_desk`
  → HANDOFF → confirmation turn.
- `bot_stuck_triggers_handoff` (tag: handoff, safety_net, f6, adversarial) — loop
  exhaustion → `bot_failed` mail → END.

---

## How to dispatch an agent against a front

1. Pick the front. Paste its **owned files** as the `files:` scope.
2. Check the matrix — don't co-launch two ⚠️ fronts on the same seam.
3. Each front's done-gate is the same: **`make verify`** (ruff + format +
   `mypy --strict` + pytest) green, plus `make mock-eval` if the change touched
   the spine (F2/F3-states/F6-as-tool).
4. After the front lands, run F7 (eval/test pass) as the regression gate.
