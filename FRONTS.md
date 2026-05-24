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
| **F6** | **Mail + Calendar** *(PLANNED — not built)* | Outbound email + calendar sync after a booking | `src/prosper/integrations/**` *(new package, see §F6 spec)* |
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

## §F6 spec — Mail + Calendar (planned, not yet built)

Motivating feature: after a confirmed booking, email the caller a confirmation
and push a calendar event. **There is no email/calendar code in the repo today**
(verified: never existed in git history, not deleted, not in `FUTURE.md`).

### Files (new)

```
src/prosper/integrations/
  __init__.py
  mail.py       # NotificationSender Protocol + NoopMailer + (later) SMTP/SendGrid
  calendar.py   # CalendarSync Protocol + NoopCalendar + (later) Google Calendar
tests/integrations/
  test_mail.py
  test_calendar.py
```

### Seam — dispatcher post-booking hook (NOT a new LLM tool)

Recommended design: make mail/calendar an **automatic side-effect**, not an LLM
tool. After `create_appointment` returns `Ok` **and** the confirmation read
succeeds, the dispatcher fires the integrations. This keeps it **off the spine**
(no `tools.py`/`flows.py`/`ALLOWED_TOOLS` edit) — so F6 only touches its own
package + one call site in `dispatcher.py` (F2 seam S3).

Mandatory properties (mirror the console bus, `CLAUDE.md` telemetry rule):

- **Fire-and-forget, never breaks the call path.** Wrap every send in
  `try/except`; a mail/calendar failure must never surface to the caller or
  abort the booking. The EHR write is the source of truth; notification is best-effort.
- **Async, no added latency.** Schedule on the loop; do not `await` a slow SMTP
  round-trip inside the turn (hard rule 7).
- **PII-safe.** Reuse `observability/redact.py` for any logging; secrets via env
  (`PROSPER_SMTP_*`, `PROSPER_GCAL_*`), never committed; respect the SSRF posture
  in `SECURITY.md`.

Alternative (only if product wants the bot to *offer* "want me to email you?"):
expose it as an LLM tool → then it **does** hit S1 (registry + `ALLOWED_TOOLS`
+ eval scenario). Decide before building; default to the side-effect design.

### Tests

- Unit: a `FakeMailer`/`FakeCalendar` that records calls; assert the hook fires
  on `Ok` booking and **swallows** a raised exception without propagating.
- If it becomes an LLM tool: add an `evals/scenarios.py` scenario (hard rule 4).

---

## How to dispatch an agent against a front

1. Pick the front. Paste its **owned files** as the `files:` scope.
2. Check the matrix — don't co-launch two ⚠️ fronts on the same seam.
3. Each front's done-gate is the same: **`make verify`** (ruff + format +
   `mypy --strict` + pytest) green, plus `make mock-eval` if the change touched
   the spine (F2/F3-states/F6-as-tool).
4. After the front lands, run F7 (eval/test pass) as the regression gate.
