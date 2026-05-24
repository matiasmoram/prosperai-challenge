# F6 — Mail + Calendar — design spec

> Status: approved in brainstorming, 2026-05-24. Front: **F6 (Mail + Calendar)**,
> `FRONTS.md`. Module home: `src/prosper/integrations/`. Next step: the
> implementation plan (`docs/superpowers/plans/2026-05-24-f6-mail-calendar.md`).
> No code is written from this document directly.

## 1. Problem

Two gaps, one surface.

**(a) Promises with no artifact.** The bot already *promises* human handoffs it
never performs — `prompts.py` says "Our front desk can help… would you like me
to take a message?" (line 42), "escalate by offering to transfer the caller to a
person" (line 78). Nothing is recorded; no one is notified. A hallucinated-success
pattern — the class of failure the eval suite hunts for — wearing good-UX clothes.

**(b) No confirmation trail.** After a successful booking the caller gets a verbal
"you're all set" and nothing durable. A real clinic emails a confirmation and the
appointment shows on a calendar.

This feature (front F6, "Mail + Calendar") gives both a real, visible artifact: a
staff-facing **Mail** surface that emulates the clinic's outbound mail, plus a
**Calendar** of booked appointments. It is a *demo* emulation — no SMTP / Gmail
API — but every message is genuinely produced, persisted, and displayed.

## 2. Two channels, one surface

| Channel | Recipient | Trigger | Spine |
|---|---|---|---|
| **Booking confirmation** | the **caller** | automatic dispatcher side-effect after `create_appointment` Ok | **off-spine** (no `tools.py`/`flows.py` edit) |
| **Staff handoff** | the **front desk** | LLM tool `leave_message_for_front_desk` + dispatcher safety-net | **on-spine** (new tool + `HANDOFF` state) — crosses S1/S3 |

Both write a `MailMessage` to one `MailStore`. The Mail pane renders both,
filterable by kind. The Calendar pane reads the EHR.

## 3. Goals / non-goals

### Goals
- Durable, full-PII outbound-mail records for booking confirmations (to caller)
  and human handoffs (to front desk), shown in a staff Mail pane.
- A last-resort handoff that fires even when the LLM is the broken component.
- A calendar view of booked appointments incl. the visit reason (`notes`).
- Reuse existing console infra patterns; never add latency to or risk the call path.

### Non-goals (YAGNI — explicitly cut)
- Real email delivery (SMTP / Gmail). The Mail pane is a demo emulation, labelled.
- A faked external calendar push (Google Calendar). The calendar is an EHR
  read-view (source of truth); a real external sync is future work behind that
  read model. Faking a "synced!" status would be a mock disguised as real
  (CLAUDE.md hard rule 1) — explicitly refused.
- Inbound replies, auth / multi-user beyond the documented trust tier, editable
  calendar, model-based urgency triage.
- Emergency *detection* robustness (pre-existing, separately tracked; see §6).

## 4. Escalation categories (handoff channel)

Decided in brainstorming:

| `category` | Trigger |
|---|---|
| `prescription` | Refills, dosage changes, new prescriptions. |
| `insurance_billing` | Coverage, payment, copays. |
| `records` | Lab results, referrals, records requests. |
| `medical_followup` | Clinical question, **only after** the bot redirected, offered to book, and the caller declined the booking *and* asked for a person. Medical questions never auto-escalate. |
| `other` | Explicit "I want to talk to a human". |
| `bot_failed` | **Safety-net only** — emitted by the dispatcher, never by the LLM (§5.3). |

## 5. Trigger engine

### 5.1 Booking confirmation (off-spine side-effect)

After `create_appointment` returns `Ok` and the dispatcher transitions `booked`,
it fire-and-forgets a `MailMessage(kind="booking_confirmation")` to the caller,
built from the Ok result (appointment id, start/end, provider) + the verified
patient. No LLM tool, no `tools.py`/`flows.py` edit. Mirrors FRONTS.md §F6's
recommended side-effect design and its mandatory properties: fire-and-forget,
async, never breaks the call path, PII handled at the staff tier.

### 5.2 Staff handoff (on-spine LLM tool)

New tool, whitelisted post-identity in `CHOOSE_INTENT` / `BOOK_FLOW` /
`CANCEL_FLOW` / `RESCHEDULE_FLOW`:

```
leave_message_for_front_desk(
    category: "prescription" | "insurance_billing" | "records"
             | "medical_followup" | "other",
    summary: str,
    callback_wanted: bool,
) -> Result[Ok, Err]
```

- Returns `Result[Ok, Err]` (rule 2).
- **Identity is not an LLM argument** — patient name + phone come from
  `SessionMemory.identified_patient`; the LLM cannot hallucinate the contact.
- Whitelisted but **intercepted in the dispatcher** (not a `HANDLERS` entry) —
  it needs `SessionMemory` + the `MailStore`, not the `EHRClient`.
- On `Ok`: write a `MailMessage(kind="handoff")`, emit the `handed_off` outcome,
  transition to `HANDOFF` (terminal). The bot may confirm only after `Ok` (rule 5).
- **Identity-first**: an unidentified caller is identified first (existing flow);
  the tool is whitelisted only post-identity, so every handoff has a real number.

### 5.3 Safety-net (dispatcher, no LLM)

On inner-loop exhaustion (`llm_loop_exhausted`) the dispatcher writes a
`MailMessage(kind="bot_failed")` directly — no LLM call — so it fires when the LLM
is the broken component. Contact best-effort from memory. Fire-and-forget.

## 6. Emergency carve-out (hard boundary)

Never the mail path. `category` has no `emergency` value. The codebase already
routes emergencies to END via `suggest_specialty → medical_emergency`
(`flows.py`, audit F-011) — booking tools are physically unmounted. Persona rule:
red-flag symptoms → "call 911 / 988 now", no message, no booking. A guard eval
(`emergency_does_not_leave_message`) asserts an emergency utterance produces **no**
mail record and routes to END, not HANDOFF. Emergency *detection* robustness is a
pre-existing, separately-tracked gap (`tests/adversarial/test_emergency_not_enforced.py`);
this feature must not regress it.

## 7. Data & trust tiers

The operator console **masks** PII by invariant (`console/events.py`
`mask_name`/`mask_phone`; audit redacts transcript text). A receptionist callback
or a caller confirmation is useless masked. The mail feed is therefore a
**different trust tier** and must not reuse the masked console bus.

### 7.1 MailStore (full-PII, staff tier)
- `src/prosper/integrations/mail.py` — `MailMessage` dataclass + `MailStore`
  (append-only JSONL at `data/mail/<session_id>.jsonl`, override
  `PROSPER_MAIL_ROOT`), full PII, intentionally un-masked. Staff tier: behind auth
  in production, loopback in the demo.
- `MailMessage` fields: `ts`, `session_id`, `kind`
  (`booking_confirmation`|`handoff`|`bot_failed`), `to_label` (e.g.
  `reception@prosper.health` or the caller's name), `subject`, `body`
  (pre-rendered plain text shown in the UI), `patient_name`, `patient_phone`,
  `category` (handoff only; `""` otherwise).

### 7.2 Calendar (EHR truth)
- New read endpoint `GET /appointments?from=&to=` in `ehr/api.py` (owned by F1 —
  coordinate), repository join `Appointment`+`Patient`+`Provider`+`Slot`,
  `status='scheduled'` in the window, returning patient name, provider, specialty,
  start/end, duration, `notes`.
- The **visit reason needs no capture work** — `create_appointment` already accepts
  `notes` (tools.py:631), the persona already sets it (prompts.py:312), persisted
  to `Appointment.notes`. Only the read endpoint is new.

## 8. Web surface

New staff page on the **existing console uvicorn** (`:7861`), under `/frontdesk/*`
(kept distinct from `/console` so masked-vs-full-PII tiers don't blur):
- `GET /frontdesk` — single-page app (Mail + Calendar tabs).
- `GET /frontdesk/mail` — JSON list of `MailMessage` (newest first).
- `GET /frontdesk/appointments?from=&to=` — calendar entries, proxied from the EHR
  via an injected async fetcher (no hard EHR import in the router).
- `GET /frontdesk/static/*` — assets, `no-store` like the console.

### 8.1 Live updates
The Mail pane **polls** `GET /frontdesk/mail` every ~2 s (decided). No SSE/bus
machinery. A message appears within ~2 s during a demo.

### 8.2 Layout
```
┌─ Prosper · Front Desk ──────────────────  [ simulated — demo ] ─┐
│  [ Mail (4) ]   [ Calendar ]                                    │
├──────────────────────────┬──────────────────────────────────────┤
│  MAIL                     │   ✉  To: reception@prosper.health     │
│ ┌──────────────────────┐ │      Re: Callback — Jane Doe          │
│ │● handoff · Jane Doe   │ │      ─────────────────────────────    │
│ │  Callback — Jane Doe  │ │   <rendered body text>                │
│ ├──────────────────────┤ │   Phone : (202) 555-0142              │
│ │  confirmation · M.Ruiz│ │   Logged: 2026-05-24 14:08 ET         │
│ │  Your appt is confirm.│ │                                       │
│ └──────────────────────┘ │                                       │
└──────────────────────────┴──────────────────────────────────────┘

  CALENDAR (week)        Mon 26      Tue 27        Wed 28
   9:30   ┌──────────┐               ┌──────────┐
  10:00   │M. Ruiz   │               │Jane Doe  │
          │Dr. Chen  │               │Dr. Patel │
          │Therapy   │               │"knee pain"│
          └──────────┘               └──────────┘
```
Vanilla HTML/JS, same minimal style as `console/static`; selecting a row renders
the message (To / Subject / Body / meta).

## 9. FSM changes (handoff channel)

New terminal state `HANDOFF`:
- `flows.py`: `State.HANDOFF`; `needs_human` transitions from `CHOOSE_INTENT` /
  `BOOK_FLOW` / `CANCEL_FLOW` / `RESCHEDULE_FLOW` → `HANDOFF`; whitelist
  `leave_message_for_front_desk` in those four. `HANDOFF` → `END` on goodbye only.
- New `outcome` category `handed_off`, distinct from
  `booked`/`cancelled`/`rescheduled`/`refused`/`abandoned`.

## 10. Error handling & non-regression
- Mail write failure → logged, never raised into the call path.
- No added latency: confirmation + safety-net are fire-and-forget; the handoff
  tool awaits the write (it's a product action the bot confirms) but is wrapped so
  a failure downgrades to a logged Ok rather than crashing the turn.
- Mail `body`/`summary` are rendered as text content in the SPA (no HTML injection).
- PII never crosses into the masked console bus; stores are physically separate.

## 11. Testing

### Eval scenarios (rule 4; mock-eval friendly)
- `refill_request_leaves_message` → `HANDOFF`, mail record `kind=handoff`,
  `category=prescription`.
- `caller_asks_for_human` → `HANDOFF`, `category=other`.
- `medical_redirects_then_books` → books, no handoff.
- `medical_declines_then_human` → `medical_followup` handoff.
- `bot_failure_safety_net_handoff` → `bot_failed`, no LLM tool call.
- `emergency_does_not_leave_message` → END, no mail record.
- Existing happy-path booking scenarios still pass; a booking now also writes a
  `booking_confirmation` mail record (assert in a unit test, not a brittle eval).

### Unit
- `MailStore` round-trip, newest-first, isolated root via env.
- Booking-confirmation side-effect fires on `booked` Ok; failure is swallowed.
- Safety-net fires on loop-exhaustion without an LLM call.
- Calendar endpoint payload shape (incl. `notes`), window + `scheduled`-only.
- Router: mail list, calendar proxy, SPA serve.
- No-op path when no `MailStore` injected.

### Gates
- `make verify` clean; `make mock-eval` clean.

## 12. Files (front F6 ownership + seams)

**New (F6-owned):**
- `src/prosper/integrations/__init__.py`
- `src/prosper/integrations/mail.py` — `MailMessage` + `MailStore` + `make_message`.
- `src/prosper/integrations/router.py` — `/frontdesk` routes + `build_frontdesk_router`.
- `src/prosper/integrations/static/index.html`, `frontdesk.js`
- `tests/integrations/**`
- `docs/adr/005-f6-mail-calendar.md`

**Touched — F1 (EHR), coordinate:** `ehr/repository.py`, `ehr/schemas.py`,
`ehr/api.py`, `ehr_client.py` (calendar read).

**Touched — F2 spine (S1/S3), coordinate / sequence:** `flows.py` (HANDOFF state,
whitelist, transitions), `tools.py` (tool schema only; **not** `HANDLERS`),
`dispatcher.py` (intercept tool, confirmation side-effect, safety-net, outcome,
`MailStore` injection), `bot.py` (build store + thread through), `console/server.py`
(mount `/frontdesk`).

**Touched — F3 (prompts):** `prompts.py` (HANDOFF task message, emergency rule,
handoff guidance).

**Touched — F7 (evals):** `evals/scenarios.py`, `evals/mock_llm.py`.

**Docs:** `FRONTS.md` §F6 (rewrite to describe the as-built two-channel feature +
the S1/S3 spine crossing), `SOLUTION.md`, `CLAUDE.md`.

> **Spine note:** FRONTS.md §F6 originally specced an *off-spine* booking-confirmation
> side-effect and flagged the LLM-tool variant as "only if product wants the bot to
> offer". Product wants the staff-handoff, which **is** on-spine. So F6 now holds
> both: the off-spine confirmation AND the on-spine handoff. The on-spine half must
> be sequenced with / routed through F2 (S1: `tools.py`/`flows.py`; S3: `bot.py`)
> and carries an eval scenario (hard rule 4). Do not run it concurrently with
> another F2-spine front.

## 13. Open items for the plan
- Exact `HANDOFF` task-message + emergency persona wording (≤ 1 KB per `TASK_MESSAGES`).
- Confirm whether `TASK_MESSAGES` is keyed by `State` enum or string.
- Confirm `_record_tool_result` accepts a non-`HANDLERS` tool result.
- ADR 005 wording.
