# Architecture

Two processes, one SQLite file, one custom FSM dispatcher. Bot and EHR speak
HTTP so the boundary is testable; the dispatcher enforces a per-state tool
whitelist so the LLM cannot fire a write outside the right state.

## Process topology

```
  ┌──────────────────┐  WebRTC   ┌─────────────────────────────────────────┐
  │  Browser client  │ ────────▶ │  bot.py (Pipecat, :7860)                │
  │  (mic + speaker) │           │                                          │
  └──────────────────┘ ◀──────── │  ┌──────────────────────────────────┐    │
                                 │  │ Pipeline:                        │    │
                                 │  │  transport.input()               │    │
                                 │  │   → ElevenLabs Realtime STT      │    │
                                 │  │   → DispatcherProcessor          │◀───┼── TTFT stamp
                                 │  │   → ElevenLabs Flash v2.5 TTS    │    │
                                 │  │   → transport.output()           │    │
                                 │  └──────────────────────────────────┘    │
                                 │            │                              │
                                 │            ▼                              │
                                 │  ┌──────────────────────────────────┐    │
                                 │  │ Dispatcher (FSM)                 │    │
                                 │  │  - state, memory, transcript     │    │
                                 │  │  - per-state tool whitelist      │    │
                                 │  │  - tool rejection + retry        │    │
                                 │  └──────────────────────────────────┘    │
                                 │            │                              │
                                 │            ├──── OpenAILLMAdapter ────────┼──▶ OpenAI
                                 │            │     (tenacity retry +        │   (gpt-4o-mini
                                 │            │      fallback model)         │    + fallback)
                                 │            │                              │
                                 │            └──── EHRClient ───────────────┘
                                 └──────────────────────────│──────────────────┘
                                                            │ httpx
                                                            ▼
                                            ┌──────────────────────────────┐
                                            │  ehr/api.py (FastAPI, :8000) │
                                            │   - 7 REST endpoints         │
                                            │   - OpenAPI /docs            │
                                            └──────────────────────────────┘
                                                            │
                                                            ▼
                                            ┌──────────────────────────────┐
                                            │  repository.py               │
                                            │   (SQLAlchemy queries,       │
                                            │    SlotTakenError → 409)     │
                                            └──────────────────────────────┘
                                                            │
                                                            ▼
                                            ┌──────────────────────────────┐
                                            │  data/ehr.db (SQLite)        │
                                            │   Provider, Patient, Slot,   │
                                            │   Appointment                │
                                            └──────────────────────────────┘
```

During `make eval` the EHR is mounted **in-process** via `httpx.ASGITransport`
— no separate uvicorn, no socket round-trip, fully hermetic, ~10× faster than
subprocess-based eval runners.

## FSM state graph

```
                    ┌──────────┐
                    │ GREETING │
                    └────┬─────┘
                         │ (user speaks)
                         ▼
              ┌─────────────────────┐
              │  IDENTIFY_PATIENT   │
              │  tools:             │
              │   find_by_phone     │──┐
              │   find_by_name_dob  │  │ (no match after both)
              └─────────┬───────────┘  │
                        │ patient_found│
                        │              ▼
                        │     ┌─────────────────┐
                        │     │ REGISTER_PATIENT│
                        │     │  tools:         │
                        │     │   create_patient│
                        │     └────────┬────────┘
                        │              │ registered
                        ▼              ▼
                    ┌───────────────────────┐
                    │     CHOOSE_INTENT     │
                    └──────┬──────────┬─────┘
              wants_book   │          │   wants_cancel
                           ▼          ▼
                ┌──────────────┐  ┌────────────────────┐
                │  BOOK_FLOW   │  │   CANCEL_FLOW      │
                │  tools:      │  │   tools:           │
                │   list_avail │  │    get_upcoming    │
                └──────┬───────┘  └──────┬─────────────┘
              slot_    │                 │  appointment_
              chosen   ▼                 ▼  chosen
                ┌──────────────┐  ┌──────────────────┐
                │ CONFIRM_BOOK │  │  CONFIRM_CANCEL  │
                │  tools:      │  │   tools:         │
                │   create_appt│  │    cancel_appt   │
                └──────┬───────┘  └──────┬───────────┘
                       │ booked          │ cancelled
                       ▼                 ▼
                       ┌─────────────────┐
                       │       END       │
                       └─────────────────┘
```

**Per-state tool whitelist enforcement.** The dispatcher inspects every
`tool_call` returned by the LLM against `ALLOWED_TOOLS[current_state]`
(`src/prosper/flows.py`). A non-whitelisted call is converted into a
`tool_rejected` event in the transcript, a system note is injected into the
LLM history (`"tool X is not available in state Y"`), and the next iteration
gives the model a chance to recover. The forbidden call never reaches the
EHR.

**Memory across states.** `SessionMemory` (in dispatcher) carries
`identified_patient`, `last_slots`, and `last_upcoming_appointments` across
turns so the LLM can refer to "the third slot" or "the Tuesday appointment"
without seeing raw UUIDs (those are filtered out by `_redact_for_llm`).

## Key invariants

- The LLM never sees a UUID. Anything the bot might read aloud goes through
  `_redact_for_llm` (e.g. `list_availability_slots` returns `"available
  slots: 2026-05-21 09:00 with Dr. Patel; ..."` not `slot_id`s).
- `create_appointment` and `cancel_appointment` validate the `slot_id` /
  `appointment_id` against `SessionMemory` before reaching the EHR. A
  hallucinated id never crosses the HTTP boundary — the handler returns
  `Err(code="hallucinated_slot_id")`.
- One active appointment per slot, enforced at the DB layer by a partial
  unique index. Concurrent booking races surface as `409 slot_taken`, not 500.
- Past slots are filtered out of `list_availability_slots` results so the bot
  can't offer 8 AM at 11 AM.
