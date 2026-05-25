# Prosper Health voice agent — solution

> Reviewer-facing tour of the codebase as it stands today. **Start with §0
> (summary).** Pair this with `docs/architecture.md` (process + FSM diagrams)
> and `docs/adr/001..006` (load-bearing decisions). Companion docs:
> `docs/FEATURES.md` (exhaustive capability list), `docs/cases.md` (test-case
> catalogue), `docs/tester.md` (test machinery). Rules live in `CLAUDE.md`,
> recipes in `CONTRIBUTING.md`.

## 0. Summary

A **two-process voice agent** that answers a fictional US clinic's phone and
**identifies the caller → registers them if new → books / cancels / reschedules**
30-minute appointments across five specialties (Mon–Fri 9–17 America/New_York,
English). Built for the Prosper Technologies challenge.

**Architecture in four lines:**
- **`bot.py`** — a Pipecat pipeline (ElevenLabs Flash STT/TTS + OpenAI LLM) on
  `:7860`, driven by a **custom FSM dispatcher** (not `pipecat-flows`).
- **`ehr/api.py`** — a FastAPI + SQLite EHR on `:8000`, the **source of truth**;
  the bot reaches it over HTTP via `EHRClient`.
- **operator console** (`:7861/console`) — live SSE telemetry of every call.
- **front desk** (`:7861/frontdesk`) — staff Mail + Calendar surface over a
  durable SQLite mail store.

**The load-bearing idea:** a hand-written FSM where the **dispatcher is the only
path to tools** and enforces a per-state tool whitelist, so the LLM physically
cannot call a tool the current state forbids, never sees a raw UUID, and cannot
confirm a write that did not happen. Everything else (triage, hybrid intent
routing, mail/calendar, barge-in) hangs off that spine.

**Status:** 12 FSM states, 11 tools (9 EHR-backed handlers + 2 intercepted),
108 offline eval scenarios + ~715 tests green (`tests/` 544 + `tester/` 171);
`mypy --strict`. What is *not* built and why → §16.1.

Read order: this summary → §0.1 (what it does) → §0.2 (how the hard concerns are
handled) → §4–§7 (topology, FSM, tools, dispatcher) → the rest as needed.

## 0.1 What it does (functionalities)

The exhaustive list lives in `docs/FEATURES.md`; the headline capabilities:

- **Voice conversation** — real WebRTC call (ElevenLabs STT/TTS + OpenAI),
  warm non-robotic persona, **barge-in** handling (history truncated to what the
  caller actually heard), latency-conscious single-round-trip flows + fillers.
- **Identity gate** — find by phone, then by name+DOB; **fuzzy name
  disambiguation** (numbered read-back, caller picks); new caller → register;
  unmatched-but-insists-existing → front-desk handoff. Booking tools are
  physically unmounted until identity is set.
- **Booking** — symptom→specialty **triage** (mini-LLM) with a recommended
  duration + clinical floor; **duration negotiation** (caller may go ≥ floor,
  sub-floor is refused); adaptive slot UX (offer 2–3, never dump); **provider
  choice** when a specialty has several doctors.
- **Cancel / reschedule** — read-back + confirm; pick the right appointment from
  a numbered list; **reschedule is atomic** (single transaction, rollback on
  conflict — never cancel-then-rebook).
- **Safety & honesty** — EHR is the source of truth (confirm only from a read);
  no hallucinated IDs (handles validated against `SessionMemory`); clarify rather
  than guess; medical-emergency red flag is an FSM-enforced hard stop; graceful
  LLM-total-failure path (canned line + reception mail); front-desk handoff.
- **Staff surfaces** — operator console (live call telemetry, masked PII) +
  front-desk Mail + Calendar (full-PII staff tier, durable SQLite store):
  booking / cancellation / reschedule mail to the doctor, callback/handoff +
  failure mail to reception, and a 7-day clinic calendar.

## 0.2 How the concerns FUTURE.md flags as important are handled

`FUTURE.md` ranks the things a reviewer cares about. Where each is handled today
(and what was deliberately deferred → §16.1):

| Concern (FUTURE.md theme) | How it is handled today | Where |
|---|---|---|
| **Latency** is a feature | Single round-trip flows; regex intent fast-path avoids an LLM hop; `STATE_FILLERS` cover silence; one-shot CHOOSE_INTENT prefetch; the speculative-race *answer* documented (deferred on local SQLite) | §15, §15.1 |
| **Reliability** under provider failure | Tenacity retry + single fallback model; transport errors → typed `Err`, never a crashed turn; graceful total-failure canned line + reception mail; bot entrypoint env fail-fast | §12 |
| **No hallucinated success** | EHR is source of truth; write-tools validate handles against `SessionMemory` (hallucinated id → `Err`, no HTTP); paired state-assertion + judge eval; offline tool-receipt gate | §6, §7, §11 |
| **Identity / PII safety** | Identity gate; LLM never sees a UUID (handle redaction); console PII masking + audit redaction; SSRF-guarded EHR URL; mail filename class eliminated | §7, §8, §13 |
| **Eval quality** | Paired state+judge (ADR 003); 108 offline scenarios; adversarial + messy-human (ASR-noise) suites; golden-trace replay | §11, `docs/tester.md` |
| **Voice UX naturalness** | Barge-in truncation; clause-aware persona; clarify-don't-guess rule | §7, §10 |

The **caller call UI** (F4) and the **mail + calendar** staff surface are
described in §8.2 and §8.1.

## 1. Overview

A voice agent that books, reschedules, and cancels appointments at a
fictional US clinic over WebRTC. Two processes share one SQLite database:

- **`bot.py`** — Pipecat pipeline (ElevenLabs Flash v2.5 STT/TTS, OpenAI
  LLM) wired to a custom FSM dispatcher.
- **`src/prosper/ehr/api.py`** — FastAPI EHR backed by SQLite/SQLAlchemy.

The bot talks to the EHR over HTTP via `EHRClient` (an `httpx.AsyncClient`).
Tests and evals mount the EHR in-process through `httpx.ASGITransport` — no
socket, no port binding, fully hermetic.

## 2. Quick start

```bash
cp env.example .env       # add ELEVENLABS_API_KEY and OPENAI_API_KEY
make install              # uv sync
make seed                 # one-time
make ehr                  # terminal 1 — FastAPI on :8000
make bot                  # terminal 2 — Pipecat on :7860
# open http://localhost:7860 → Connect
```

Docker variant: `docker-compose up`.

## 3. Quick evaluate

```bash
make mock-eval            # zero-cost: all scripted scenarios via deterministic mock in ~5 s, no API key
PROSPER_EVAL_LIVE=1 OPENAI_API_KEY=… make eval            # live: full suite, paired state + judge
PROSPER_EVAL_LIVE=1 OPENAI_API_KEY=… uv run python -m evals --json evals/results/baseline.json
PROSPER_EVAL_LIVE=1 OPENAI_API_KEY=… uv run python -m evals --concurrency 4 --baseline evals/results/baseline.json
```

Gating is intentional: a missing `PROSPER_EVAL_LIVE=1` OR missing
`OPENAI_API_KEY` exits with code 2 so an account without quota fails loud
rather than silently passing. `make mock-eval` is the canonical offline
smoke test.

## 4. Process topology

```
[browser WebRTC]
       │
       ▼
   bot.py  (Pipecat pipeline + DispatcherProcessor)
       │  httpx
       ▼
ehr/api.py (FastAPI)  ──►  data/ehr.db  (SQLite + WAL)
       ▲
       │  ASGITransport (in-process, evals + tests only)
       │
  evals/runner
```

The HTTP loopback adds ~1 ms; it is dwarfed by STT/LLM/TTS, so keeping the
EHR as a separate process is free in latency and earns realism — reviewers
can hammer it independently, and it scales / fails independently from the
bot. See `docs/adr/002-separate-ehr-process.md`.

## 5. FSM

`src/prosper/flows.py` is plain data: a `State` enum, an `ALLOWED_TOOLS`
dict, a `TRANSITIONS` map. No framework. The dispatcher owns the runtime.

States today (12):

```
GREETING
  ↓ go_identify / goodbye
IDENTIFY_PATIENT  ──no_match──►  REGISTER_PATIENT
  ↓ patient_found                       ↓ registered
CHOOSE_INTENT  ◄──────────────────────────┘
  │
  ├─ wants_book ─────► BOOK_FLOW ─slot_chosen──► CONFIRM_BOOK ─booked──► END
  │   │                                              ↑ abort
  ├─ wants_cancel ────► CANCEL_FLOW ─appt_chosen──► CONFIRM_CANCEL ─cancelled──► END
  │   │                  │                              │ cancelled_then_rebook
  │   │                  └─ nothing_to_cancel ──► END   └──► BOOK_FLOW
  │   │                                              ↑ abort
  ├─ wants_reschedule ─► RESCHEDULE_FLOW ─slot_chosen─► CONFIRM_RESCHEDULE ─rescheduled──► END
  │   │                  │                              ↑ abort
  │   │                  └─ nothing_to_reschedule ──► END
  │   │
  │   └─ needs_human (from any of the four flow states above)
  │              ↓
  │           HANDOFF ─handed_off──► END   (one final LLM confirmation turn; no tools)
  │              └─ goodbye ──────► END
  │
  └─ goodbye ─► END                       (every non-END state has a goodbye edge)
```

Per-state tool whitelist enforced by the dispatcher (not by prompt
instructions — the LLM literally cannot see tools outside the whitelist):

| State | Allowed tools |
|---|---|
| GREETING | (none) |
| IDENTIFY_PATIENT | `find_patient_by_phone`, `find_patient_by_name_dob` |
| REGISTER_PATIENT | `create_patient` |
| CHOOSE_INTENT | `route_intent` *(internal)*, `leave_message_for_front_desk` *(internal)* |
| BOOK_FLOW | `list_availability_slots`, `suggest_specialty`, `leave_message_for_front_desk` *(internal)* |
| CANCEL_FLOW | `get_upcoming_appointments`, `leave_message_for_front_desk` *(internal)* |
| RESCHEDULE_FLOW | `get_upcoming_appointments`, `list_availability_slots`, `leave_message_for_front_desk` *(internal)* |
| CONFIRM_BOOK | `create_appointment` |
| CONFIRM_CANCEL | `cancel_appointment` |
| CONFIRM_RESCHEDULE | `reschedule_appointment` |
| HANDOFF | (none) |
| END | (none) |

`route_intent` is whitelisted in CHOOSE_INTENT but **dispatcher-intercepted**
(`INTERNAL_TOOLS` in `flows.py`, `Dispatcher._handle_route_intent`) — it is the
hybrid-navigation tool: the LLM proposes the caller's intent
(`wants_book` / `wants_cancel` / `wants_reschedule`) and the dispatcher validates
the edge. It has no `HANDLERS` entry and fires no EHR call. `suggest_specialty`
(BOOK_FLOW) is a real handler backed by the triage mini-LLM (ADR 005, §6); its
`medical_emergency` Err drives the hard `BOOK_FLOW → END` edge so the booking
tools are physically unmounted on a red flag (audit F-011).

`leave_message_for_front_desk` is whitelisted in all four post-identity flow
states but is **also dispatcher-intercepted** (absent from `HANDLERS`; same
pattern as `route_intent`). When the LLM calls it, the dispatcher builds a
`MailMessage` from `SessionMemory` identity (never from LLM-supplied arguments,
preventing spoofing), writes it to `MailStore`, and fires the `needs_human →
HANDOFF` transition. HANDOFF is a terminal holding state: the dispatcher
suppresses further tool loops (same as END) but generates one final LLM turn so
the bot can speak a warm handoff confirmation before the call ends. The
`_outcome_published` flag on the dispatcher prevents a double `outcome` event
when the subsequent `goodbye → END` edge fires. Stuck-detector safety-net: when
the inner LLM loop exhausts all 4 iterations, `_emit_safety_net_handoff` fires a
`bot_failed` mail so staff are alerted even when no `needs_human` edge triggered.

If the LLM tries a tool outside the whitelist, the dispatcher records a
`tool_rejected` transcript entry and feeds a synthetic error
`role:tool` message back into the LLM's history so the model retries on
the next inner iteration. Forbidden calls never reach an HTTP boundary.

See `docs/adr/001-hybrid-fsm-with-tool-whitelist.md` for the design
choice; `docs/architecture.md` for the diagram.

## 6. Tools

Nine EHR-backed handlers in `src/prosper/tools.py` (the `HANDLERS` map), plus
`route_intent` and `leave_message_for_front_desk` — both whitelisted but
dispatcher-intercepted, no handler, no EHR call (see §5). All handlers return
`Result[Ok[dict], Err]` (`src/prosper/result.py`). The `Err.code` strings are
**public eval contract** — scenarios assert on them, so renaming a code is a
breaking change and must update `evals/scenarios.py` in the same commit.

| Tool | Whitelisted in | Err codes |
|---|---|---|
| `route_intent` *(internal — no handler)* | CHOOSE_INTENT | — (dispatcher validates the FSM edge) |
| `leave_message_for_front_desk` *(internal — no handler)* | CHOOSE_INTENT, BOOK_FLOW, CANCEL_FLOW, RESCHEDULE_FLOW | — (dispatcher writes MailMessage from SessionMemory, fires `needs_human → HANDOFF`) |
| `find_patient_by_phone` | IDENTIFY | `ehr_error` |
| `find_patient_by_name_dob` | IDENTIFY | `dob_unparseable`, `ehr_error` |
| `create_patient` | REGISTER | `dob_unparseable`, `patient_exists`, `ehr_error` |
| `suggest_specialty` | BOOK_FLOW | `medical_emergency`; passes through `llm.classify_symptoms`: `triage_unavailable`, `unknown_specialty`, `invalid_duration` |
| `list_availability_slots` | BOOK_FLOW, RESCHEDULE_FLOW | `date_unparseable`, `invalid_duration`, `ehr_error` |
| `create_appointment` | CONFIRM_BOOK | `missing_slot_id`, `missing_patient_id`, `hallucinated_slot_id`, `patient_id_mismatch`, `below_minimum_safe_duration`, `slot_taken_other_patient`, `no_consecutive_slots`, `patient_or_slot_not_found`, `ehr_error` |
| `get_upcoming_appointments` | CANCEL_FLOW, RESCHEDULE_FLOW | `ehr_error` |
| `cancel_appointment` | CONFIRM_CANCEL | `missing_appointment_id`, `hallucinated_appointment_id`, `appointment_not_found`, `ehr_error` |
| `reschedule_appointment` | CONFIRM_RESCHEDULE | `missing_appointment_id`, `missing_slot_id`, `hallucinated_appointment_id`, `hallucinated_slot_id`, `slot_taken_other_patient`, `appointment_or_slot_not_found`, `ehr_error` |

`suggest_specialty` maps a free-form symptom string → `{specialty,
duration_minutes, confidence, follow_up}` via the triage mini-LLM
(`llm.classify_symptoms`, a single `gpt-4o-mini` JSON-mode call). A red flag
returns `Err(medical_emergency)`; visit duration ∈ {30, 60, 90} flows into
`list_availability_slots`/`create_appointment` (multi-slot lock). Full design:
ADR 005.

Three things to know about the tool layer:

1. **`reschedule_appointment` is atomic.** Single transaction, single
   `PATCH` to `/appointments/{id}/slot`. Replaces the older
   cancel-then-rebook chain — if the new slot is held, the original
   appointment is preserved (no orphan window). The dispatcher chains
   the legacy `cancelled_then_rebook` path only when the caller flipped
   intent *mid-cancel* (e.g. said "reschedule" while already inside
   CANCEL_FLOW). Plain "reschedule" from CHOOSE_INTENT routes straight
   to the atomic path.

2. **`list_availability_slots` has a 6-day forward-scan fallback.** When
   the asked date is empty, the handler probes day+1..day+6 with the
   same specialty filter and returns the first hit under
   `next_day_with_slots`. The dispatcher transitions to CONFIRM_BOOK on
   either path and `_resolve_memory_handles` validates against the
   fallback slots so `create_appointment` doesn't reject them as
   hallucinated. Scenario `availability_falls_through_to_next_day`
   pins this behaviour.

3. **`specialty` filter.** `list_availability_slots` accepts an
   optional `specialty` string ("Therapist", "Dermatologist",
   "Psychiatrist", "General Practice", "Physiotherapist"). Today the
   LLM picks the string from caller intent; a mini-LLM router that
   maps complaint → specialty is **in flight** (§14). Scenarios
   `specialty_filter_therapist`, `specialty_unknown_falls_back`, and
   `specialty_no_filter_any_doctor` cover the three branches.

## 7. Dispatcher mechanics

`src/prosper/dispatcher.py` is intentionally small (~1200 LOC) so a
reviewer can read the whole machine in one sitting. Five things it
does beyond the FSM:

1. **Per-state LLM request.** `_messages_for_llm` returns
   `[system: CLINIC_PERSONA, system: TASK_MESSAGES[state], …history]`.
   Persona stays stable across turns (prompt-cache target); task
   message swaps per state.

2. **Handle redaction (audit A3).** `_redact_for_llm` strips raw UUIDs.
   Lists are enumerated `[1] 10:00 with Dr. Patel; [2] 10:30 …`. The
   LLM passes the bracketed number back as `slot_id` / `appointment_id`
   and `_resolve_memory_handles` swaps it for the real UUID before any
   HTTP call. The LLM has never seen and cannot read aloud an internal
   id.

3. **Session memory.** `SessionMemory.{identified_patient, last_slots,
   last_upcoming_appointments, wants_reschedule}`. Every tool response
   that returns a list updates the corresponding memory field;
   `_validate_against_memory` then rejects any write-tool call whose
   `slot_id` / `appointment_id` is not in that memory
   (`hallucinated_slot_id`, `hallucinated_appointment_id`). This is
   defence-in-depth on top of the EHR's own constraints.

4. **History sliding window + orphan pruning.** `_HISTORY_WINDOW = 40`.
   When the window slices off an assistant message that announced a
   `tool_call_id`, any `role:tool` reply whose id no longer has a
   parent is pruned in `_prune_orphan_tool_messages`. Without this,
   OpenAI rejects the next turn with
   `messages with role tool must be a response to a preceding message
   with tool_calls` (see `ERRORS.md` E1).

5. **Per-turn tool dedup.** A misbehaving model can fire the same tool
   with identical args repeatedly. After two duplicate calls in one
   turn the dispatcher injects a `BLOCKED:` synthetic response telling
   the model to speak to the user instead. Inner-loop budget is 4
   iterations — exhaustion drops a `FALLBACK_LINES["llm_loop_exhausted"]`
   line so the caller never hears dead air.

`bot.py` wires the dispatcher into Pipecat via `DispatcherProcessor`,
which consumes `TranscriptionFrame` directly (the default
`LLMUserAggregator` adds a 1 s aggregation tax we don't want; see the
note at the top of `bot.py`). A last-resort `try/except` around
`handle_user_turn` keeps a mid-call exception from killing the WebRTC
session — the caller hears "sorry, I missed that" and the dispatcher
recovers.

## 8. Operator console

`src/prosper/console/` ships a live operator pane fed by the dispatcher.
Nine event types from `events.py`:

`state_change`, `transcript_turn`, `tool_call_start`, `tool_call_end`,
`latency_tick`, `patient_identified`, `slots_offered`, `outcome`,
`turn_interrupted`.

Wiring:

- `dispatcher._publish(type, payload)` is a fire-and-forget call against
  an injected `ConsoleBus`. When no bus is passed (tests / eval runner)
  every publish site is a hard no-op.
- The bus uses bounded queues with overflow-drop semantics so a slow SSE
  client cannot back-pressure the call path.
- **Bus + audit + mail are built on every live call, never gated.** In
  `bot.py::run_bot` the `ConsoleBus`, `AuditJSONLWriter`, and `MailStore` are
  constructed unconditionally and `audit.attach(bus)` runs for every call, so
  `data/audit/<session>.jsonl` and `data/mail/mail.db` always persist. Only the
  embedded uvicorn console server (`:7861`) is gated on `PROSPER_CONSOLE_ENABLED`
  (`serve_console`): `make run-all` sets it to `0` so the bot does not race the
  standing console for the port, yet mail still reaches the front-desk inbox and
  every call is recorded for the standing console to list, replay, and live-tail.
  (Earlier this wiring lived inside `if console_enabled:`, so run-all silently
  dropped mail and recorded nothing.)
- `_redact_tool_args` and `mask_name` / `mask_phone` enforce PII redaction
  on every payload before it hits the bus — the operator UI sees masked
  values, the transcript and audit log keep originals.
- `_publish_outcome` distinguishes `booked` / `cancelled` / `refused`
  (offer made and declined) / `abandoned` (caller dropped pre-confirm).
  Confusing the last two poisons clinic dashboards, so the categorisation
  is explicit.

**Live, follow, and replay.** Three SSE paths feed the same renderers:

- `/console/stream/{id}` tails the in-process live bus — only meaningful in the
  *embedded* console (the bot's own process). The standing `run-all` console has
  no in-process bus from the bot subprocess, so this path stays empty there.
- `/console/replay/{id}` replays a recorded session from the audit JSONL, paced
  by the recorded gaps, terminated by a `replay_complete` sentinel.
- `/console/replay/{id}?follow=1` **live-tail**: emits the events already on disk
  instantly, then polls the growing audit file (`AuditJSONLWriter.tail_events`,
  `PROSPER_CONSOLE_FOLLOW_POLL_S`, default 0.5s) and emits new events as they
  land. It self-closes with `replay_complete` when the call's `outcome` event is
  written. This is how the standing `run-all` console follows an in-progress
  call without an in-process bus. A finished session passed to `follow=1`
  behaves exactly like replay (the `outcome` is already on disk).

Opening `/console` with **no** session id shows an **idle landing** and then
**polls `/console/sessions`**; when the newest session's audit file was modified
within a live window (~12 s), the client auto-follows it (`follow=1`) so an
in-progress call appears and updates without operator action, returning to idle
when it ends. It does NOT blindly auto-replay a stale recording (which would
look like a live call). Selecting a past session (or `/console/<id>`) opens it in
follow mode too — instant for a finished recording (ends on its `outcome`),
live-updating if the call is still going — behind an amber **REPLAY** banner when
the stream is a paced replay rather than live/follow.

Design spec: `docs/superpowers/specs/2026-05-20-operator-console-design.md`
+ `docs/adr/004-operator-console-event-stream.md`.

## 8.1. Front-desk mail + calendar surface (F6 — shipped Wave 4)

`src/prosper/integrations/` is a separate trust tier from the masked
operator-console bus — it holds full patient PII for staff, not redacted
telemetry for an operator screen.

### MailStore

`integrations/mail.py` (`MailStore`) persists `MailMessage` rows to a single
unified SQLite store at `data/mail/mail.db` (one `mail` table) — one coherent
inbox across every call, durable across restarts, **not** per-session files. It
is deliberately a separate database from the EHR (distinct full-PII trust tier).
`write` offloads the blocking insert via `asyncio.to_thread` so it never stalls
the bot's event loop. Five mail kinds — the three appointment-lifecycle events
plus handoff + failure:

| Kind | Trigger | Source of identity | Addressed to (`to_label`) |
|---|---|---|---|
| `handoff` | LLM calls `leave_message_for_front_desk` → `needs_human` transition | `SessionMemory` exclusively (never LLM args) | `Reception` |
| `booking_confirmation` | `create_appointment` returns `Ok` → `_emit_booking_confirmation` | `SessionMemory.identified_patient` | the booked provider (`Dr. X`) |
| `cancellation` | `cancel_appointment` returns `Ok` → `_emit_cancellation_notice` | `SessionMemory.identified_patient`; provider/start recovered from `last_upcoming_appointments` (cancel returns only `{ok, appointment_id}`) | the freed provider (`Dr. X`) |
| `reschedule` | `reschedule_appointment` returns `Ok` → `_emit_reschedule_notice` | `SessionMemory.identified_patient`; new start/provider from the result | the new provider (`Dr. X`) |
| `bot_failed` | inner LLM loop exhausts (`_emit_safety_net_handoff`) or total LLM failure (`_emit_system_failure_mail`) | `SessionMemory.identified_patient` | `Reception` |

The three lifecycle emitters share `_caller_identity()` + `_fire_mail()`; the
atomic reschedule fires exactly one `reschedule` mail, while a *mid-cancel
intent-flip* (cancel-then-rebook chain) correctly fires a `cancellation` then a
`booking_confirmation` — two mails for two real DB writes.

The inbox is a **staff surface**, so every message reads as a short note to a
person — `Reception` for callbacks/failures, the booked/freed `Dr. X` for a
booking / cancellation / reschedule notification — with a one-line subject +
body. All writes are **fire-and-forget**
via `_inflight_publishes` (same strong-ref pattern as console-bus publishes); a
write failure is logged but never propagates to the call path.

### /frontdesk router + SPA

`integrations/router.py` exposes a `/frontdesk` FastAPI router served on the
console uvicorn (`:7861`). It reads the full-PII `MailStore` — patient name,
phone, call summary, callback flag — for clinic receptionists returning calls.
This is a deliberate higher-trust tier than the masked operator-console bus.

In the demo the console uvicorn binds to `127.0.0.1` (loopback-only) so no
inbound internet path exists. In production this endpoint must sit behind
authentication (clinic SSO or shared-secret header) before being exposed beyond
localhost. See `SECURITY.md` for the threat note.

The router is wired in `console/server.py::build_app`; it is included only when
both `store` and `calendar_fetch` are supplied (guarded in `bot.py` under the
console-enabled gate). `bot.py` constructs a `MailStore()` and an async
`calendar_fetch` closure (thin wrapper over `EHRClient`) and passes both to
`Dispatcher` and `build_frontdesk_router`. The SPA's own assets are served by an
explicit `GET /frontdesk/static/{filename}` `FileResponse` route (traversal-guarded)
— a `router.mount(StaticFiles)` on a *prefixed* APIRouter does not route, so the
JS would 404 and the page render as an inert shell.

**Persistent standing site.** Because the bot only mounts `/frontdesk` per
WebRTC connection, `scripts/frontdesk_server.py` runs the surface as an always-on
site for staff: default mode reads the real `data/mail/` (where a running bot
appends mail mid-call) + proxies the live EHR calendar, so a mail sent during a
call appears within the SPA's 2 s poll; `--demo` mode serves an isolated seeded
EHR + sample mail with no other process running. The calendar renders as a 7-day
grid (one event block per appointment, coloured by specialty).

### handed_off outcome on the bus

When `needs_human` fires the `HANDOFF` transition, `_publish_outcome` emits
`outcome = "handed_off"` on the console bus. The `_outcome_published` flag
prevents a second emission when the subsequent `goodbye → END` would otherwise
fire again. `tester/receipt_gate.py` classifies `handed_off` in `NO_CLAIM` — it
asserts a mail write, not an EHR tool receipt.

ADR: `docs/adr/006-handoff-state.md`.

## 8.2. Caller call UI (F4 — the phone-call page)

`src/prosper/console/static/call/` (`index.html`, `call.js`, `call.css`) is the
**caller-facing WebRTC phone UI** — the page a patient "calls" from. It is served
as static assets on the console uvicorn at `:7861/call` (mounted by
`sse.py::mount_static_on_app`), but it runs **no backend of its own**: `call.js`
dials the Pipecat runner directly at `BOT_ORIGIN` (`window.PROSPER_BOT_ORIGIN`,
default `http://127.0.0.1:7860`) via `POST /api/offer` and negotiates an
`RTCPeerConnection` (mic in, bot audio out).

Properties worth noting: a state-aware dial button (idle / connecting / live /
ended / error) with accessible labels (`aria-pressed`, `aria-label`); a
user-actionable mic-permission error ("Allow mic access in your browser
settings"); and `BOT_ORIGIN` overridable for deployment so it is not hard-pinned
to localhost. Ownership boundary: F4 edits **static assets only** — a new mount
path or route is an F5 change to `server.py`/`sse.py` (seam S2 in `FRONTS.md`),
and the `:7860` signaling pipeline is F2 (`bot.py`, seam S3).

## 9. EHR

`src/prosper/ehr/`:

- `models.py` — SQLAlchemy: `Patient`, `Provider`, `Slot`, `Appointment`.
  Two load-bearing constraints:
  - **`Provider.specialty`** column (auto-migrated on startup since
    commit `3266b02` — see `db.py`).
  - **Partial unique index** on `Appointment(slot_id) WHERE
    status='scheduled'`. Concurrent booking races surface as `409
    slot_taken` (never 500), and a recoverable Err code propagates up.
- `repository.py` — single LEFT-OUTER-JOIN to list availability (was N+1
  pre-fix; bench dropped from 115 ms → ~10 ms — see
  `docs/bench-results.md`).
- `schemas.py` — Pydantic with `Field(max_length=…)` caps on every
  string field so a malicious POST can't load a multi-megabyte string
  into SQLite via the request parser.
- `api.py` — FastAPI app. Endpoint mapping vs the challenge spec:

| Challenge name | REST endpoint |
|---|---|
| `create_patient` | `POST /patients` |
| `find_patient` (by name + DOB) | `GET /patients/by-name-dob?name=&dob=` |
| `find_patient` (by phone — our addition) | `GET /patients/by-phone?phone=` |
| `list_availability_slots` | `GET /availability?date=&provider_id=&specialty=` |
| `create_appointment` | `POST /appointments` |
| `cancel_appointment` | `POST /appointments/{id}/cancel` |
| reschedule (atomic) | `PATCH /appointments/{id}/slot` |
| (helper) patient appointments | `GET /patients/{id}/appointments` |
| health | `GET /health` |

**Slot datetimes are naive UTC** (seed converts NY-local → UTC → strips
tzinfo). Any new code reading `slot.start_at` must follow the same
convention; mixing naive-local with naive-UTC silently misreads
availability.

**Past slots are filtered server-side** (`repository.list_availability_slots`)
so the bot cannot offer 8 am at 11 am.

## 10. Prompts and persona

`src/prosper/prompts.py`:

- `CLINIC_PERSONA` (~1100 tokens) — long on purpose. OpenAI's prompt
  cache kicks in at 1024+ stable tokens; the dispatcher reports
  `cached_prompt_tokens` per turn so the eval runner can show
  `cache=XX%` per scenario. **Do not trim** the persona to chase token
  cost; you'd lose more from a missed cache hit than you'd save.
- `TASK_MESSAGES[state]` — ≤ 1 KB per entry, enforced by
  `test_each_task_message_under_1_kb`. Bot guidance for the state lives
  here; the dispatcher injects today's date so the model can resolve
  "tomorrow" / "next Tuesday".
- `STATE_FILLERS` — short "one moment" lines pushed as a TTS frame
  before a tool-firing state so the caller never hears silence during
  the LLM round-trip.
- `FALLBACK_LINES["llm_loop_exhausted"]` — last-resort speech when the
  4-iteration inner loop exhausts without a real reply.

All caller-audible strings live here. Inline voice strings in `bot.py`
or `dispatcher.py` are a regression — they bypass the 1 KB gate and
fragment the cache.

## 11. Eval suite

Runner: `evals/__main__.py` + `evals/runner.py`. Each scenario asserts
on **both**:

- **State delta (deterministic).** DB row counts before/after, expected
  tool codes fired, forbidden tools didn't, FSM reached
  `expected_terminal_state`, and a regex post-check for hallucinated
  confirmations ("I've cancelled" with no matching `tool_ok`).
- **LLM judge (semantic).** Full transcript scored against
  natural-language `judge_criteria`. PASS only if both checks pass.

The paired check exists because the judge alone marked some adversarial
scenarios as PASS even when the bot said "I've cancelled that" without
any `tool_ok`. See `docs/adr/003-paired-state-and-judge-eval.md`.

### Scenarios

`evals/scenarios.py` defines 108 scenarios. The tag buckets below are
**representative, not exhaustive** (a scenario can carry several tags); the
canonical list is the file itself, run via `make mock-eval`:

| Tag | Scenarios | What's tested |
|---|---|---|
| happy | `new_patient_books`, `existing_patient_cancels`, `cancel_picks_from_list`, `caller_volunteers_email_at_registration`, `availability_falls_through_to_next_day`, `reschedule_existing_appointment`, `reschedule_multi_appointment_picks_second`, `specialty_no_filter_any_doctor`, `specialty_filter_therapist` | Mandatory flows + the "everything went right" reschedule and specialty paths |
| recovery | `dob_misheard_then_corrected`, `patient_correction_mid_register`, `phone_correction_mid_register`, `dob_unparseable_then_recovery` | Caller corrects a field mid-turn; bot must adopt the latest value |
| edge | `slot_taken_by_other`, `cancel_when_nothing_to_cancel`, `phone_format_chaos`, `slot_handle_out_of_range`, `availability_falls_through_to_next_day`, `goodbye_at_*` | Boundary behaviour: race, empty list, parsing chaos, dispatcher handle guard |
| adversarial | `prompt_injection_direct_override`, `prompt_injection_stored_in_name`, `cross_patient_cancel_refusal`, `hallucinated_confirmation_trap`, `multi_turn_drift_hallucinated_slot`, `off_topic_steering_and_budget`, `rude_caller_still_completes_booking`, `insurance_question_redirect`, `hallucinated_appointment_id_in_reschedule`, `reschedule_cross_patient_refusal` | Injection, authorization, hallucination, tone, scope |
| abandon | `goodbye_at_greeting`, `goodbye_at_identify`, `goodbye_at_choose_intent`, `goodbye_mid_confirmation`, `reschedule_abort_at_confirm`, `goodbye_at_book_flow`, `goodbye_at_cancel_flow`, `goodbye_at_reschedule_flow` | Caller hangs up at every reachable state (incl. mid-book/cancel/reschedule) — no orphan writes, no fake confirmations |
| reschedule | `reschedule_existing_appointment`, `reschedule_no_upcoming_appointments`, `reschedule_cross_patient_refusal`, `reschedule_abort_at_confirm`, `reschedule_multi_appointment_picks_second`, `hallucinated_appointment_id_in_reschedule` | Atomic move + every refusal/abort path on it |
| specialty | `specialty_unknown_falls_back`, `specialty_no_filter_any_doctor`, `specialty_filter_therapist` | Filter pass-through, unknown specialty graceful decline, no-filter wildcard |
| hybrid | `ambiguous_intent_routed_via_tool`, `route_intent_resolves_to_cancel`, `route_intent_resolves_to_reschedule`, `cancel_then_rebook_intent_flip` | The `route_intent` navigation tool (CHOOSE_INTENT) end-to-end + the cancel→rebook intent-flip chain |
| identity | `identify_by_name_dob_disambiguation` | Two fuzzy candidates → caller's pick resolves to the correct patient (F-013 class, end-to-end) |

Adding a scenario is a ~20-line PR per `CONTRIBUTING.md` — pure data
(`Scenario` dataclass), no framework changes.

### Mock-LLM mode (`make mock-eval`)

`evals/mock_llm.py` is a deterministic ~1700 LOC harness:

- `MockDispatcherLLM` plays a per-scenario script of `LLMReply` objects;
  it knows about dispatcher `__use_first_slot__` / `__use_upcoming_n__`
  sentinels so the script doesn't have to hard-code UUIDs.
- `MockPersonaLLM` replays canned caller utterances.
- `mock_judge_transcript` returns PASS unconditionally — mock mode tests
  the *runner*, not the judge.
- A `_END_NOW_MARK` sentinel force-transitions the dispatcher into
  `State.END` for refusal scenarios (no tools fire, no natural END
  transition). The real bot reaches END via `cancelled` / `booked` /
  `nothing_to_*` / `goodbye`.

Runs the whole suite in ~5 s offline. CLI flags:

- `--concurrency N` (default 4) — per-scenario isolated SQLite engine,
  cuts wall-clock ~3× without crossing transactional state.
- `--baseline previous.json` — fails the run with exit 3 if a
  previously-passing scenario regressed.
- `--mock-llm` — swap in the deterministic mock LLM.
- `--only NAME` — run a single scenario.

### Catching hallucinations without hand-dialing (eval strategy)

The challenge asks specifically for *ways to automatically test or simulate
calls so hallucinations and agent mistakes are caught without dialing in by
hand*. The field converges on one shape — a **synthetic caller** (LLM with
persona + goal) talks to the agent, scored by **deterministic state/tool
assertions + an LLM judge** — and the deterministic half must stay the hard
gate, because LLM-simulated callers drift off-goal (arXiv *"Lost in
Simulation"*). That is exactly the paired `state_pass AND judge_pass` design
already in place (ADR 003).

We organise the work as a cost-tiered pyramid; the right-hand column is where
this repo sits:

| Tier | Method | Cost | Status here |
|---|---|---|---|
| every commit | FSM/handle invariants, golden-trace replay, scripted mock scenarios | $0 offline | shipped (`evals/`, `trace_replay.py`, `tester/`) |
| every commit | **property-based FSM fuzzing** (Hypothesis `RuleBasedStateMachine`) | $0 offline | planned (see §14) |
| every commit | **tool-receipt hallucination gate** | $0 offline | shipped (`tester/`) |
| broad coverage | **autonomous adversarial persona caller** (persona+goal+stop) | cheap (tokens) | shipped (`tester/simulate.py`) |
| nightly | live persona-sim + judge, `pass^k` reliability, latency/cost gate | $ tokens | partial (`evals/sim.py`, `judge.py`, `eval-baseline`) |
| pre-release | full audio loop TTS→agent→STT + noise/accent/barge-in | $$ telephony | intentionally cut (§16) |

The prototypes off this brainstorm live in **`tester/`** (build order chosen by
an LLM council; see `tester/README.md`):

- **Tool-receipt gate** (`tester/receipt_gate.py`). A standing invariant over
  the operator-console event stream, the NABAOS *tool-receipt* pattern at its
  smallest. The terminal `outcome` event (`booked` / `cancelled` /
  `rescheduled`) is a **claim**; each `tool_call_end` with `outcome=="ok"` is a
  **receipt**. A positive claim with no matching receipt earlier in the same
  session is a `Violation`. This guards the FSM's *outcome accounting* — a
  different surface from the spoken-text regex in
  `runner._check_hallucinated_confirmation`, which it complements.
- **Recorder** (`tester/recorder.py`). `RecordingBus` + `record_call()` drive a
  scenario through the offline mock LLMs with a capturing bus attached, closing
  the gap that nothing in the harness consumed the bus as an eval oracle.
- **Offline tests** (`tester/test_receipt_gate.py`). Unit tests on hand-built
  tampered streams, plus an integration check that **every** mock scenario backs
  its outcome with a real write. Green today; goes red the moment a refactor lets
  a positive outcome fire without its tool. `make tester`; folded into
  `make verify`.
- **Autonomous call simulator** (`tester/simulate.py`, `personas.py`,
  `live_sim.py`, `invariants.py`). The literal *simulate-calls-without-dialing*
  piece: the LLM plays a goal-seeking adversarial **caller** (persona + goal +
  stop condition, generating each turn) against the **real bot**; a
  `RecordingBus` captures the call and `check_call()` audits it for the two
  hallucination invariants (unbacked outcome + spoken false confirmation). A
  curated persona library (confused elderly, wrong-then-corrected DOB, mid-call
  cancel→reschedule flip, prompt-injection name, demands an unoffered slot,
  rude-but-completes, off-topic, hallucination bait) plus `--generate N` to have
  the LLM invent fresh ones. An adversarial caller *not getting its way is not a
  failure* — only a dishonest confirmation is; exit non-zero == a real
  hallucination caught. Live (`make simulate`, needs `OPENAI_API_KEY`); the
  pytest smoke test is gated on `PROSPER_EVAL_LIVE=1` so `make verify` stays
  free. Validated: the injection persona books normally (override ignored) and
  the bait persona is refused a fake "it's cancelled" — both PASS.
- **Messy-human / ASR-noise layer** (`tester/noise.py`, `tester/clarification.py`).
  Real callers are disfluent and STT mishears them; the bot must re-prompt or
  confirm, never silently act on a garbled value. `noise.py` is a pure, *seeded*
  injector — disfluencies (fillers, repetitions, false starts; Shriberg model)
  and ASR errors (homophones, number-word swaps like fifteen↔fifty, the dangerous
  intent flip cancel↔schedule, dropped day-numbers, phone format drift; hamming.ai
  taxonomy) composed by `garble(text, seed, profile)`. A `Persona.noise_profile`
  garbles every caller turn before the bot hears it. `clarification.py` is the
  **contract**: `check_recovers_gracefully()` flags `plowed_ahead_on_garble` when a
  write tool consumes a garbled turn with no intervening clarification/confirm —
  decidable offline from the event stream (no LLM needed for the assert; there is
  deliberately no fake dispatcher confidence gate since we are text-in/text-out).
  The clarification *behaviour* already lives in `prompts.py` (the clarification
  rule + `FALLBACK_LINES`); this suite supplies the messy input + the assertion.
  Deterministic core runs in `make verify`; 4 MESSY personas run live.

## 12. Reliability

Seven layers, all in `src/prosper/llm.py`, `bot.py`, `dispatcher.py`:

1. **Tenacity retry on transient LLM failures.** `AsyncRetrying`, 3
   attempts, exponential jitter. Only retries `APIConnectionError`,
   `APITimeoutError`, `InternalServerError`, `RateLimitError`. Auth
   errors are NOT retried.
2. **Fallback model.** `PROSPER_BOT_FALLBACK_MODEL` env var. On primary
   exhaustion, one last call against the fallback (e.g. `gpt-4o-mini`
   brownout → `gpt-4o`).
3. **Startup health-check.** `_startup_health_check()` pings EHR
   `/health` before accepting clients. Soft-fail with loud warning so
   the operator sees the failure pre-call.
4. **Env fail-fast.** `load_dotenv(override=False)` (external env wins
   in containers/CI) then a required-vars check that exits with
   `SystemExit(2)` *before* the 17 s pipecat / silero / onnxruntime
   import wall when `PROSPER_BOT_ENTRYPOINT=1`.
5. **History sliding window + orphan pruning.** §7 item 4.
6. **`try/except` around `handle_user_turn`.** A stray exception is
   caught, logged, and answered with a recovery line. The WebRTC
   session survives.
7. **EHR client lifecycle via `async with`.** `async with
   dispatcher._ehr:` in `run_bot` — SIGTERM, transport crash, or a
   startup-time exception still close the client. The previous
   lifecycle (close inside the disconnect handler) leaked sockets on
   every abnormal termination.

Tests: `test_adapter_retries_on_transient_5xx_then_succeeds`,
`test_adapter_falls_back_to_secondary_model_after_retries_exhausted`,
`test_bot_dispatcher_processor.py`, `test_dispatcher_gaps.py`.

Explicitly deferred: STT/TTS multi-provider fallback (pipecat issue
[#4139](https://github.com/pipecat-ai/pipecat/issues/4139)),
pre-recorded "everything is on fire" TTS fallback.

## 13. Security

Three guards on the network + log surface:

1. **SSRF guard on `PROSPER_EHR_URL`.** `_validated_ehr_url` in
   `bot.py` rejects any scheme outside `{http, https}` and any URL
   without a hostname before building the EHR client. A compromised
   `.env` cannot repoint the bot at `169.254.169.254` (cloud metadata)
   or an internal admin endpoint. Hostname allowlisting beyond this is
   the egress firewall's job.
2. **PII redaction in log lines (HIPAA-adjacent).**
   `src/prosper/observability/redact.py` masks US-shape phone numbers,
   DOB-like strings (ISO + US + dotted), and emails in every
   `USER:` / `BOT[state]:` line. UUIDs are stashed first so the phone
   regex doesn't eat their digit-rich interior. `mask_name` is exposed
   for structured name fields. The dispatcher and transcript still see
   raw text — only `journalctl` / `loguru` sinks are masked. Not a
   substitute for a proper de-identification pipeline; good enough to
   keep ops-tier log readers from seeing caller contact details.
3. **DoS caps on request bodies.** Every Pydantic schema in
   `ehr/schemas.py` carries `Field(max_length=…)`.

Audit trail: `docs/research/2026-05-20-security-audit.md` +
`SECURITY.md`.

## 14. In-flight work

Active work the main branch does not yet reflect:

- **Mini-LLM specialty router — SHIPPED (ADR 005, 2026-05-23).** No longer
  in-flight. Landed as the `suggest_specialty` tool (BOOK_FLOW) backed by
  `llm.classify_symptoms` (a `gpt-4o-mini` JSON-mode call), variable visit
  duration {30/60/90} via an `AppointmentSlotLock` junction table, and a hard
  `medical_emergency → END` FSM edge. The hybrid-navigation `route_intent` tool
  (CHOOSE_INTENT, dispatcher-intercepted) landed alongside it. See §5, §6, ADR
  005. Scenarios `symptom_routes_to_gp`, `symptom_ambiguous_followup`,
  `direct_specialty_skips_triage` pin it; mocked offline by `MockTriageClient`.
- **F6 Mail + Calendar (handoff + booking-confirmation + safety-net) — SHIPPED
  (ADR 006, 2026-05-25).** No longer in-flight. Landed as: `State.HANDOFF`
  terminal holding state; `leave_message_for_front_desk` dispatcher-intercepted
  tool; `MailStore` (five mail kinds: handoff / booking_confirmation /
  cancellation / reschedule / bot_failed — lifecycle parity added 2026-05-25,
  commit `5185dfd`); `/frontdesk` router + SPA on the console uvicorn; `handed_off`
  outcome on the bus; stuck-detector safety-net (`_emit_safety_net_handoff`);
  booking-confirmation fire-and-forget (`_emit_booking_confirmation`); two new
  eval scenarios (`caller_requests_human`, `bot_stuck_triggers_handoff`). See
  §5, §6, §8.1, ADR 006.
- **Barge-in / interruption handling — SHIPPED (Wave 7, 2026-05-25).**
  `TTSAudibleObserver` (`src/prosper/observers.py`) sits between the TTS
  service and `transport.output` in the Pipecat pipeline. On
  `InterruptionFrame` it flushes the accumulated `TTSTextFrame` buffer and
  calls `dispatcher.mark_last_assistant_interrupted(spoken_text)`, which
  truncates `history[-1]` to the audible portion and appends
  `" [INTERRUPTED by user]"` so the next LLM call sees an honest timeline.
  `CLINIC_PERSONA` already contains the annotation explanation. VAD is tuned
  for barge-in (`confidence=0.35`, `min_volume=0.15`, `start_secs=0.1`).
  Unit tests in `tests/test_barge_in.py` cover partial-text truncation,
  empty-buffer `[NOT HEARD]` prefix, idempotency, empty-history no-op, and
  non-assistant-last-turn no-op (N-002). Real-time pipeline behaviour
  (live audio InterruptionFrame) requires staging verification.
- **Speculative race.** `docs/research/speculative_race.md` — research
  notes on overlapping STT partials with speculative LLM kickoff to
  reduce TTFT. Not yet wired.
- **Property-based FSM fuzzer (`tester/`).** Next prototype after the
  tool-receipt gate and the autonomous call simulator (§11). A Hypothesis
  `RuleBasedStateMachine` over the
  dispatcher generates random-but-valid caller-action programs (`@rule` =
  give phone / give DOB / pick slot / change mind) and asserts the FSM
  invariants on every dialog — tool ∈ `ALLOWED_TOOLS[state]`, no UUID
  reaches the LLM, handle round-trip, no double-book,
  confirm-only-after-successful-write. Finds whole bug *classes* via
  shrinking rather than one hand-written scenario at a time.

The eval suite pins both the plain specialty-filter behaviour
(`specialty_filter_therapist`, `specialty_unknown_falls_back`,
`specialty_no_filter_any_doctor`) and the shipped complaint→specialty router
(`symptom_routes_to_gp`, `symptom_ambiguous_followup`,
`direct_specialty_skips_triage`).

## 15. Latency (README bonus #1)

Shipped:

- **`TimingCollector` (`src/prosper/observability/timing.py`)** —
  every `_llm_turn` and `_execute_tool` wrapped in an async
  `measure(phase=, state=)` context. Emits one JSON span per call
  (`{"evt":"span","phase":"llm","state":"BOOK_FLOW","duration_ms":820}`),
  aggregates p50 / p95 / max at session end via `format_table()`.
- **TTFT.** `DispatcherProcessor` stamps `_stt_end_ts` on each final
  `TranscriptionFrame` and records `phase="ttft"` after the dispatcher
  returns. Canonical voice-agent metric in 2026; treated as a
  first-class phase in the table.
- **`cached_prompt_tokens` surfacing.** `OpenAILLMAdapter._single_call`
  pulls `usage.prompt_tokens_details.cached_tokens` and the dispatcher
  tracks both per-turn and call totals
  (`cached_prompt_tokens_total`, `prompt_tokens_total`).
- **ElevenLabs Flash v2.5** — first-audio latency ~75 ms vs ~200 ms on
  the default constructor.
- **Filler speech** in tool-firing states — `"One moment."` /
  `"Let me check."` / `"Looking that up."` rotated so the caller hears
  acknowledgement immediately rather than dead air during the LLM
  round-trip.
- **SQLite WAL** + `expire_on_commit=False` — write contention from
  ~30 ms → ~8 ms.
- **Lazy Silero VAD import** in `bot.py` — saves ~4 s of cold-import
  when `PROSPER_BOT_ENTRYPOINT=1` short-circuits.

EHR endpoint micro-bench (`make bench` → `scripts/bench.py`, 10 rounds
against `make ehr` on local NVMe):

```
endpoint           min   p50   p95   max
health             1.8   2.1   2.7   2.9
availability       8.8   9.7  10.7  11.2
by-phone (hit)     4.2   4.5   5.2   5.3
by-phone (miss)    4.1   4.6   4.9   5.7
by-name-dob        4.5   5.3   5.6   6.6
```

LLM dominates by an order of magnitude — EHR calls (httpx loopback or
ASGITransport) stay sub-15 ms; a single LLM turn is 800–2000 ms.

### 15.1 Speculative execution on call start — *the latency answer (deferred, by design)*

The highest-leverage *structural* latency idea is to overlap backend I/O with
the caller's speech: the moment a call connects (and again the moment a name is
heard), speculatively warm the paths the call is likely to take instead of doing
them sequentially on demand. Concretely:

- On `GREETING → IDENTIFY`, fire the identity lookup as soon as a name/phone is
  heard, and (for an existing patient) pre-fetch their upcoming appointments —
  so by the time the caller states an intent, "do you have anything to
  cancel/reschedule?" is already answered.
- Pre-stage the three intent branches (book / cancel / reschedule) in parallel
  rather than choosing one and then starting its I/O.
- For a new caller, prepare the registration payload speculatively (but **never**
  speculatively `create_patient` — our EHR has no name-dedupe, so a speculative
  write could duplicate a patient).

**Why it is deferred (not skipped):** on the current in-process SQLite EHR a
lookup is ~5–15 ms, so racing it saves <200 ms while adding real asyncio
task-lifecycle + cancellation complexity in the dispatcher core (drain/cancel on
`no_match` / intent-flip / call end, the MarioW333 pattern). The cost/benefit
only flips when the EHR moves out-of-process / remote (round-trips in the
100–300 ms range), where the overlap pays for the complexity. The groundwork is
already in `speculation.py` (`next_n_business_days`, fuzzy disambiguation
shipped); the async prefetch itself is held. **Scope (light warm-path vs full
3-branch race) to be decided by an LLM-council pass before building.** Tracked in
`FUTURE.md` §3.3.

## 16. Intentional cuts (deferred on purpose)

| Cut | Why |
|---|---|
| No auth / HIPAA encryption at rest | Demo scope. SQLite is dev-only. Upgrade path: Postgres + managed token service. Log-line PII redaction covers the most-likely leak surface in the meantime. |
| No multi-provider STT/TTS fallback | Pipecat has no first-class `ServiceSwitcher` (issue #4139). We ship LLM retry+fallback as the higher-value win. |
| No streaming TTS flush-after-each-clause | Biggest remaining perceived-latency win, but needs a custom Pipecat processor — out of scope for the submission window. |
| No OpenRouter / generic LLM gateway | Simplifies fallback to one env-var swap and unlocks 100+ models. Kept provider-direct so OpenAI's prompt-cache discount still applies. |
| No real audio smoke test (TTS → STT loop) | Current `evals/audio_smoke/` just asserts the dispatcher module imports; a real round-trip needs recorded WAVs + ElevenLabs credits in CI. |
| No proactive prefetch on STT partials | Brittle on partial-text changes; `ttft` phase is instrumented so we'll see the real pain before adding. |
| No `AvailabilityCache` | A 30-line dict TTL cache would shave the ~10 ms `list_availability_slots` cost — far below the LLM-dominated budget, so deferred. |
| No pre-recorded "everything is on fire" TTS fallback | Needs a checked-in WAV + regex phone capture; deferred behind the LLM retry layer that handles 99% of provider blips. |

## 16.1. Discarded / deferred — `FUTURE.md` adjudication

Every item proposed in `FUTURE.md` that is **not** implemented, with the explicit
decision. "Deferred-by-design" = a reasoned no for this submission, not an
oversight; "partial" = the high-value half shipped, the rest is a small follow-up;
"not pursued" = reasonable next work, just not done. The shipped items
(1.1, 2.1, 2.3, 5.1, 6.1, 6.2, and the fuzzy-disambiguation half of 3.3) are
recorded in `CHANGELOG.md` and the relevant SOLUTION sections.

| FUTURE item | Decision | Why |
|---|---|---|
| **1.2** STT/TTS multi-provider fallback | **deferred** | No first-class Pipecat `ServiceSwitcher` (issue #4139). LLM retry + fallback model shipped as the higher-value reliability win. |
| **1.3** `AvailabilityCache` (60 s TTL) | **discarded for now** | Saves ~10 ms on `list_availability_slots` — far below the LLM-dominated budget. Revisit only when the EHR moves remote (100–300 ms round-trips). |
| **2.2** Multi-model judge w/ disagreement | **not pursued** | Paired state-assertion + single judge (ADR 003) already gates; a second judge model adds cost for marginal signal in a demo. |
| **3.1** Streaming TTS (flush-after-clause) | **deferred** | Biggest remaining perceived-latency win, but needs a custom Pipecat frame processor — out of the submission window. |
| **3.2** Slot prefetch on STT partials | **discarded for now** | Brittle on noisy partial text; `ttft` is instrumented so the real pain is measurable before adding speculative EHR calls. |
| **3.3** Async speculative race (full) | **deferred by design** | The fuzzy-disambiguation half **shipped** (`speculation.py`). The async prefetch itself saves <200 ms on local SQLite vs. real asyncio-cancellation complexity; revisit when remote. Scope (light warm-path vs full 3-branch race) wants an LLM-council pass first. |
| **4.1** Provider preference capture/routing | **partial** | Doctor **choice** offering shipped (Wave 8 — bot offers providers when a specialty has 2+). The session-memory `preferred_provider_id` + automatic availability filtering is **not** built — small, low-risk follow-up. |
| **4.2** Reason-for-visit / notes capture | **partial** | `notes` is plumbed through `create_appointment` + the EHR schema + a `book_appointment_with_notes` scenario. The proactive "anything you'd like the doctor to know?" ask + `pending_notes` pass-through is **not** wired — small follow-up. |
| **5.2** Input-validation hardening (OWASP A03) | **not pursued** | `Field(max_length=…)` DoS caps shipped. E.164 `PhoneStr`, DOB plausibility bounds, and HTML-strip on `notes` are **not** — a reasonable next security increment (low risk, ~S effort). |

## 17. Future work (priority order)

1. **Speculative race** (§14) — STT partials → speculative LLM kickoff.
2. **STT/TTS multi-provider fallback** — blocked on pipecat #4139.
3. **Streaming TTS** via ElevenLabs flush-after-each-clause.
4. **OpenRouter as LLM gateway** — one env-var swap, 100+ models.
5. **Audio smoke tests** with a real TTS → STT loop.
6. **Continuous production eval** — 5–10 % sampling of live transcripts
   to the LLM judge for drift detection.
7. **Pre-recorded "everything is on fire" TTS fallback** for the
   double-failure case.
8. **`AvailabilityCache`** with 60 s TTL in `repository.py`.
9. **Property-based FSM fuzzer** (Hypothesis `RuleBasedStateMachine`) — §14.

Already landed (was on this list): mock-eval offline mode, parallel
eval runner, atomic reschedule, specialty filter, next-day forward
scan, operator console event stream, **mini-LLM specialty router +
hybrid `route_intent` navigation (ADR 005)**, **barge-in / interruption
handling (TTSAudibleObserver + mark_last_assistant_interrupted, Wave 7)**.

## 18. File map

- `src/prosper/ehr/` — FastAPI app, SQLAlchemy models, repository,
  schemas, db engine (auto-migration for `Provider.specialty`).
- `src/prosper/dispatcher.py` — FSM runtime, transcript, tool-whitelist
  enforcement, handle redaction, memory validation, history pruning,
  console bus wiring.
- `src/prosper/flows.py` — state graph topology + per-state tool
  whitelist (plain data).
- `src/prosper/tools.py` — 9 tool handlers + `TOOL_SCHEMAS` (OpenAI
  function-calling shapes) + `HANDLERS` map.
- `src/prosper/prompts.py` — `CLINIC_PERSONA`, per-state
  `TASK_MESSAGES`, `STATE_FILLERS`, `FALLBACK_LINES`. All caller-
  audible strings.
- `src/prosper/llm.py` — `OpenAILLMAdapter` (implements
  `LLMClientProtocol`); retry, fallback model, usage surfacing,
  `classify_symptoms` triage call, per-request timeout.
- `src/prosper/ehr_client.py` — `EHRClient` (httpx) with X-Request-Id
  threading + SSRF-validated base URL.
- `src/prosper/result.py` — `Result[Ok, Err]` discriminated union.
- `src/prosper/speculation.py` — fuzzy identity disambiguation
  (`classify_find_result`) + `next_n_business_days` (speculative-race groundwork).
- `src/prosper/observers.py` — `TTSAudibleObserver`: barge-in capture of the
  audible TTS prefix, feeds `mark_last_assistant_interrupted`.
- `src/prosper/observability/timing.py` — `TimingCollector` + JSON
  span logs.
- `src/prosper/observability/redact.py` — `redact_pii`, `mask_name`,
  `mask_phone`.
- `src/prosper/console/` — `events.py` (9 event types), `bus.py`
  (bounded async queues + overflow drop), `sse.py`, `server.py`,
  `audit.py` (JSONL writer + `tail_events` for follow/replay), `_utils.py`,
  `static/` (operator console + `call/` caller UI).
- `src/prosper/integrations/` — F6 staff tier: `mail.py` (`MailStore`,
  unified `mail.db`, 5 kinds), `router.py` (`/frontdesk` router), `static/`
  (Mail + Calendar SPA).
- `src/prosper/bot.py` — Pipecat pipeline wiring, `DispatcherProcessor`
  (transcript aggregation/debounce), SSRF guard, env fail-fast, lazy VAD
  import, barge-in (`allow_interruptions`), telemetry/mail wiring.
- `evals/` — `Scenario` / `StateExpectation` types, `PersonaSimulator`,
  judge, runner (parallel, baseline), CLI.
- `evals/mock_llm.py` — deterministic mock LLM + persona scripts.
- `tester/` — third test surface (F7): `receipt_gate.py` (tool-receipt
  hallucination gate), `recorder.py`, `simulate.py`/`personas.py`/`live_sim.py`
  (autonomous adversarial caller), `noise.py`/`clarification.py` (messy-human /
  ASR-noise). See §11 + `docs/tester.md`.
- `scripts/run_all.py` — one-command orchestrator: launches EHR + bot +
  standing console/frontdesk together (the "deploy everything" entry).
- `scripts/frontdesk_server.py` — always-on front-desk site (real `data/mail/`
  + live EHR calendar; `--demo` for an isolated seeded copy).
- `scripts/sim_call.py` — drive book/cancel/reschedule through the real
  dispatcher + EHR + MailStore, printing the calendar + mail delta.
- `scripts/scaffold_scenario.py` — scenario-from-transcript scaffolder.
- `scripts/console_harness.py` — console smoke harness.
- `scripts/bench.py` — EHR endpoint micro-bench.
- `scripts/status.py` — one-shot repo health snapshot (`make status`).
- `scripts/seed.py` — seed `data/ehr.db` (5-week slot grid + bulk appointments;
  auto-runs on empty DB).
- `docs/adr/001..006` — ADRs: hybrid FSM, separate EHR process, paired
  eval, operator console, symptom triage, handoff state.
- `docs/architecture.md` — ASCII process + FSM diagrams.
- `docs/bench-results.md` — pinned bench snapshots.
- `docs/glossary.md` — terminology cheat-sheet.
- `docs/research/` — codebase-audit, security-audit, prod-readiness,
  reliability, eval-depth, latency-advanced, perf-wave2, interruption
  design, speculative race.
- `docs/superpowers/specs/2026-05-19-prosper-challenge-design.md` —
  full deliberation trail (LLM council verdict per decision).
- `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`, `ERRORS.md`,
  `FUTURE.md`, `.editorconfig`, `.gitattributes` — repo hygiene +
  contributor surface.
