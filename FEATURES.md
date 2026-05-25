# FEATURES.md — what the bot does

A high-level map of everything the Prosper voice agent does today. For *how* any
of it is wired, see `SOLUTION.md` (the section-by-section reference); for *why* a
load-bearing decision was made, see `docs/adr/`. This file is the "grosso modo"
inventory — capabilities, not internals.

## The bot in one line

A two-process voice agent (Pipecat bot `:7860` ↔ FSM dispatcher ↔ FastAPI EHR
`:8000` on SQLite) that answers a US clinic's phone, identifies the caller,
registers them if new, and **books / cancels / reschedules** 30-minute
appointments across five specialties — business hours Mon–Fri 9–17
America/New_York, in English.

## 1. Conversation & telephony

- **Real voice pipeline**: ElevenLabs STT/TTS + OpenAI LLM over Pipecat WebRTC
  (the `/call` page dials `:7860` directly).
- **Natural, non-robotic persona** — a long cached clinic persona, per-state task
  messages, varied refusal/clarification *shapes* (never canned scripts).
- **Barge-in / interruption handling** — when the caller talks over the bot, the
  assistant history is truncated to what was actually spoken (TTS-audible
  observer) so the bot doesn't "remember" saying something the caller never heard.
- **Latency as a feature** — single-round-trip flows, `STATE_FILLERS` cover silence
  rather than blocking; a regex fast-path routes clear intents without an extra LLM call.

## 2. Identity (a hard checkpoint)

- **Find by phone**, then **find by name + DOB** if phone misses.
- **Fuzzy name disambiguation** — when name+DOB returns multiple near-matches, the
  bot reads them back numbered and resolves "the first one" / "number two".
- **New-caller path** — not found → "have you been here before?" → still unmatched
  → **front-desk handoff** (no dead-end); genuinely new → register.
- **Registration** of new patients (name, DOB, phone).
- Booking / cancel / reschedule tools are **physically unavailable** until identity
  is established (per-state tool whitelist).

## 3. Booking

- **Symptom → specialty triage** via a mini-LLM (`suggest_specialty`): maps a
  complaint to one of Therapist / Psychiatrist / General Practice / Dermatologist /
  Physiotherapist, with a recommended **visit duration** (30/60/90) and a
  **clinical floor** (`minimum_safe_minutes`).
- **Duration negotiation** — the caller may choose any duration **≥ the clinical
  floor**; a sub-floor request is nudged once then re-offered at the floor (a
  dispatcher guard rejects sub-floor bookings: `below_minimum_safe_duration`).
- **Adaptive slot UX** — asks rough day/time-of-day preference, proposes 2–3 free
  slots (never dumps the full list), confirms before committing.
- **Provider choice** — when a specialty has multiple providers, the bot offers a
  doctor choice.
- **Multi-slot durations** — 60/90-minute visits lock 2/3 consecutive 30-min slots.

## 4. Cancel & reschedule

- **Cancel** — reads back the specific appointment (picks from a numbered list when
  the caller has several) and confirms before cancelling.
- **Reschedule is atomic** — a single transaction with rollback-on-conflict; if the
  new slot is taken, the original appointment is preserved (never cancel-then-rebook).
- **Pinpointing** — cancel/reschedule identify the *right* appointment (by provider,
  ordinal, date) before any write.
- **Intent routing** — hybrid: a regex fast-path for decisive phrasings + an
  LLM `route_intent` tool that disambiguates reschedule-vs-cancel-vs-book for
  ambiguous utterances; the FSM validates every proposed transition.

## 5. Safety & honesty (the load-bearing properties)

- **EHR is the source of truth** — every caller-facing confirmation comes from a
  read after the write; a tool returning `Err` → apologise, never confirm.
- **No hallucinated IDs** — the LLM never sees a UUID; slot/appointment handles are
  enumerated `[1] [2]` and validated against session memory, so a hallucinated id
  returns `Err` without ever touching HTTP.
- **Clarify, don't plow ahead** — on unclear/garbled input the bot asks one short
  clarifying question; it never guesses identity, which appointment to cancel, or a time.
- **Medical-emergency red flag** — a triage red flag is an FSM-enforced hard stop
  (no booking), not just advice.
- **Graceful LLM-failure path** — if the model call fails completely (retries +
  fallback model exhausted), the caller hears a canned line and a reception mail is
  filed — never dead air, never a crash.
- **Front-desk handoff** — when the bot is stuck or the caller asks for a human, it
  files a handoff message and ends gracefully (HANDOFF state).

## 6. Front desk: mail + calendar (F6)

- **Booking-confirmation mail**, **handoff messages**, and **bot-failure alerts**
  written to a durable full-PII store (staff tier), surfaced at `/frontdesk`.
- **Clinic calendar** — `GET /appointments` over a date range for a day/week view.
- All mail writes are **fire-and-forget** — they never add latency or break the call.

## 7. Operator console (live monitoring)

- A `:7861` dashboard streaming **typed telemetry events** (state changes,
  transcript turns, tool start/end, latency ticks, patient-identified, slots-offered,
  outcome, turn-interrupted) over SSE.
- **PII never reaches the bus raw** — names/phones masked, args redacted, durable
  audit log scrubbed; bounded queues drop on overflow so telemetry can't back-pressure a call.
- **Outcome accounting** — booked / cancelled / rescheduled / refused / abandoned /
  handed_off categories for clinic analytics.

## 8. EHR backend

- FastAPI + SQLAlchemy on SQLite; seeded with **10 providers (2 per specialty)** and
  a varied patient set, weekday-only slots.
- **One active appointment per slot** enforced by a partial unique index — races
  surface as `409 slot_taken`, never a 500.
- **Past slots filtered server-side**; **naive-UTC** datetime convention throughout.
- Input-validation caps (`Field(max_length=…)`) guard against multi-MB POST → DoS.

## 9. Reliability & security (summary)

- Tenacity retry + single fallback model on transient LLM errors; SSRF-guarded EHR
  base URL; PII redaction before the LLM and before logs; loopback-gated `/frontdesk`;
  mail filename sanitisation. Full threat model in `SECURITY.md`.

---

For the full feature list incl. in-flight work see `SOLUTION.md` §14 and `FUTURE.md`.
Test coverage of these features is catalogued in `cases.md`; the test machinery
(AI + non-AI) is described in `tester.md`.
