# ADR 006 — HANDOFF state + front-desk mail integration

**Date:** 2026-05-25
**Status:** Accepted
**Supersedes:** (none)

## Context

The Prosper voice agent can book, cancel, and reschedule appointments autonomously.
However, a class of caller requests falls outside the bot's scope: prescription
refills, insurance/billing questions, lab results, records requests, and callers
who explicitly want to speak to a person. Before this ADR, the bot had no structured
way to hand these off — it either tried to book (wrong) or gave a vague apology
with no follow-up path.

A secondary concern is graceful degradation: when the bot's inner LLM loop exhausts
(4 iterations, all tool calls blocked or repeated), the caller heard silence. There
was no safety net that alerted staff to the failed call.

## Decision

### State.HANDOFF (terminal)

A new terminal state `HANDOFF` is added to the FSM (`flows.py`). It is reachable
from any of the four post-identity flow states (`CHOOSE_INTENT`, `BOOK_FLOW`,
`CANCEL_FLOW`, `RESCHEDULE_FLOW`) via the `needs_human` transition edge. Like
`END`, `HANDOFF` exposes no tools, but it generates one final LLM turn before
transitioning to `END` on the next `goodbye` (or hanging up). This gives the
bot one spoken sentence to confirm the hand-off before the call closes.

### leave_message_for_front_desk (dispatcher-intercepted tool)

A new `leave_message_for_front_desk` tool is whitelisted in the four flow states.
It is deliberately **absent from `HANDLERS`** — the dispatcher intercepts it
because it needs `SessionMemory` (identity comes from the verified caller, not
LLM arguments) and the injected `MailStore`, not the EHR client. This is the
same pattern as `route_intent`.

The tool accepts `category` (enum: `prescription`, `insurance_billing`, `records`,
`medical_followup`, `other`), `summary` (one short sentence for staff), and
`callback_wanted` (boolean). The dispatcher builds a `MailMessage` whose
`patient_name` and `patient_phone` come exclusively from `SessionMemory`, never
from LLM-generated arguments, preventing identity spoofing.

### MailStore (full-PII staff tier)

`src/prosper/integrations/mail.py` (`MailStore`) is a separate trust tier from
the masked operator-console bus. It holds full patient PII for two channels:

- **handoff** — triggered by `leave_message_for_front_desk`.
- **booking_confirmation** — fire-and-forget side-effect on every successful
  `create_appointment` (off-spine, no FSM change).
- **bot_failed** — fire-and-forget on LLM loop exhaustion (safety net).

All mail writes are fire-and-forget via `_inflight_publishes` (same strong-ref
pattern as the console-bus tasks). A write failure is logged but never propagates
to the call path.

### Safety-net handoff on loop exhaustion

When the dispatcher's inner LLM loop exhausts (4 iterations without a free-text
turn), `_emit_safety_net_handoff` fires a `bot_failed` mail and the bot speaks
`FALLBACK_LINES["llm_loop_exhausted"]`. The FSM state does not change (the loop
exhaustion is not a `HANDOFF` transition — no `needs_human` edge fires), but
staff are alerted via the mail record.

### outcome: handed_off

A new outcome label `handed_off` is emitted when `_transition("needs_human")`
reaches `HANDOFF`. The `_outcome_published` flag prevents double-emission if
the subsequent `goodbye → END` transition would otherwise fire a second `outcome`
event. `tester/receipt_gate.py` classifies `handed_off` in `NO_CLAIM` (it asserts
a mail write, not an EHR receipt).

## Consequences

- **Additive only.** No existing `State`, `Err.code`, `ALLOWED_TOOLS` entry, or
  `TRANSITIONS` edge was removed or renamed.
- **Emergency carve-out.** The `medical_emergency` guard in
  `_maybe_transition_from_tool` runs before the `LEAVE_MESSAGE_TOOL` intercept,
  so a `suggest_specialty → medical_emergency` Err always routes to `END`, never
  to `HANDOFF`.
- **`/frontdesk` surface.** The `MailStore` is read by the `/frontdesk` FastAPI
  router (`src/prosper/integrations/router.py`), served at `/frontdesk` on the
  console uvicorn. It is staff-only (loopback-only in the demo, auth in prod).
- **Bot.py wiring.** `bot.py` constructs a `MailStore` and an async calendar
  fetcher under the console-enabled gate and passes both to `Dispatcher` and
  `build_frontdesk_router` respectively.

## Rejected alternatives

- **Supervisor agent pattern** — routing handoff to a separate LLM agent adds
  latency and complexity. The dispatcher already holds the verified identity;
  a simple mail write is sufficient.
- **Inline HANDOFF → END in one turn** — the caller would hear nothing confirming
  the hand-off. A one-hop confirmation turn is worth the extra latency.
- **Handoff as a fire-and-forget tool in HANDLERS** — the handler would need the
  `MailStore` injected into it, breaking the tool handler's contract (tools only
  receive an `EHRClient`). The dispatcher-interception pattern avoids this.
