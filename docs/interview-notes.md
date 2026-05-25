# Interview cheat-sheet

Private prep notes — answers to the ten most-likely interview questions about
this codebase. Also useful to a reviewer skimming `docs/` for evidence the
decisions were deliberate.

---

### 1. "Why did you build your own EHR instead of integrating with a real one?"

The challenge says *"build an EHR"*. Wiring up a real third-party clinical
data system would have made the EHR the unknown variable in every eval —
flaky network, surprise rate limits, fixture drift. By owning the EHR I own
its behaviour under stress (concurrent booking races, past-slot filtering,
409 idempotency), which is exactly the surface the bot has to recover from
gracefully. The repository pattern (`src/prosper/ehr/repository.py`) makes a
later swap to Healthie or any FHIR backend a single-file change.

### 2. "Why custom dispatcher and not Pipecat Flows?"

Pipecat Flows is great but adds a dependency, `NodeConfig` boilerplate, and
indirection on the latency-sensitive path. My `dispatcher.py` is ~250 LOC
end-to-end — a reviewer reads the whole FSM in one sitting. `flows.py` is
plain data (state enum, allowed-tools dict, transitions dict), so a future
migration to Pipecat Flows is mechanical. See ADR-001.

### 3. "How does your bot handle a hallucinated slot_id?"

The LLM never sees raw UUIDs (`_redact_for_llm` strips them) — but if it
fabricates one anyway, the dispatcher validates `slot_id` against
`SessionMemory.last_slots` *before* the EHR HTTP call. Mismatch returns
`Err(code="hallucinated_slot_id")`. Same defence on
`appointment_id` for cancellations. There's a unit test
(`test_dispatcher_rejects_hallucinated_slot_id`) and the scenario
`hallucinated_confirmation_trap` covers the lying-bot variant.

### 4. "What's TTFT and how do you measure it?"

Time-to-first-token, end-of-user-speech to start-of-bot-speech. It's the
canonical voice-agent latency metric in 2026 because it's what the caller
actually perceives. I stamp `_stt_end_ts` when a final `TranscriptionFrame`
arrives and record `ttft` to the `TimingCollector` once the dispatcher hands
back its reply (the LLM response is the first thing TTS will emit). The eval
CLI prints `ttft_p50` per scenario.

### 5. "Why no streaming TTS?"

Two reasons. First, ElevenLabs Flash v2.5 already cuts first-audio latency
to ~75 ms (vs ~200 ms default), which captures a big chunk of the perceived
win without restructuring the pipeline. Second, streaming requires
flush-after-each-clause logic in the dispatcher — moderate engineering work
with unknown ROI before measurement. It's #2 in the future-work list.

### 6. "What's your reliability story when OpenAI is down?"

Three layers (`src/prosper/llm.py` + `src/prosper/bot.py`):

1. **Tenacity retry** — 3 attempts, exponential jitter, scoped to
   `APIConnectionError`, `APITimeoutError`, `InternalServerError`,
   `RateLimitError`. Auth errors are NOT retried.
2. **Fallback model** — on primary exhaustion, one last call against
   `PROSPER_BOT_FALLBACK_MODEL` (e.g. gpt-4o-mini brownout → gpt-4o).
3. **EHR startup health-check** — soft-fail with loud warning so the
   operator sees the problem pre-call, not mid-conversation.

The `DispatcherProcessor.process_frame` also wraps `handle_user_turn` in a
last-resort try/except so a stray exception speaks `"Sorry, I missed that
— could you say it again?"` rather than crashing the call.

Two unit tests cover the retry + fallback paths
(`test_adapter_retries_on_transient_5xx_then_succeeds`,
`test_adapter_falls_back_to_secondary_model_after_retries_exhausted`).
STT/TTS multi-provider fallback is deferred — Pipecat has no first-class
`ServiceSwitcher` yet (upstream issue #4139).

### 7. "How does the eval suite catch a bot that says 'I cancelled it' but didn't?"

This is exactly the gap that drove ADR-003. Judge-only would have marked it
PASS because the transcript reads correctly. Two safeguards:

- **State delta check** — `cancelled_appointment_count_delta` must be 1
  for cancel scenarios; if the bot didn't actually fire `cancel_appointment`,
  the delta is 0 and the scenario fails with structured reason.
- **Regex post-check** — `_HALLUCINATED_CLAIM` scans every assistant turn
  for "I cancelled / booked" phrasing and requires a matching `tool_ok`
  within a 4-event window. Fails the scenario with reason
  `"hallucinated confirmation: '...'"`.

The adversarial scenario `hallucinated_confirmation_trap` exercises exactly
this path.

### 8. "What would you do differently with another week?"

In priority order (from `SOLUTION.md` Future work):

1. **STT/TTS multi-provider fallback** — Deepgram for STT, OpenAI TTS as
   secondary. Blocked on Pipecat `ServiceSwitcher` (issue #4139).
2. **Streaming TTS** — biggest remaining perceived-latency win.
3. **OpenRouter as LLM gateway** — single env-var fallback story.
4. **Real audio smoke tests** — current skeleton just asserts the module
   imports; want a real TTS→STT loop.
5. **`mypy --strict` in pre-commit / CI.**
6. **Continuous production eval (5–10% sampling)** for drift detection.

### 9. "Walk me through phone-first identification end to end."

`GREETING` → user speaks → `IDENTIFY_PATIENT`. Bot asks for phone. LLM
calls `find_patient_by_phone(phone)` — tool handler normalises to E.164,
hits `GET /patients/by-phone`. Three branches:

- **1 match:** dispatcher updates `memory.identified_patient`, reads back
  the name ("I have you as Ada Lovelace — is that right?"), and transitions
  to `CHOOSE_INTENT`.
- **0 matches:** bot falls back to name+DOB ("OK, can I get your full name
  and date of birth?"), LLM calls `find_patient_by_name_dob`. Still 0 →
  transitions to `REGISTER_PATIENT`.
- **>1 matches:** ask for DOB to disambiguate before transitioning.

Phone-first because phone is a digit string — STT handles it crisply. Name
and DOB have many spoken forms (`"ten five"` vs `"October fifth"`). The
challenge-required name+DOB lookup is fully implemented and exercised on
the fallback path — one scenario covers each.

### 10. "Why pre-seeded Slot rows instead of computed availability?"

Three reasons:

1. **Idempotency becomes a unique constraint, not a distributed lock.**
   Partial unique index on `slot_id WHERE status='scheduled'` — one active
   appointment per slot, enforced at the DB. Concurrent booking races
   surface as `409 slot_taken` instead of a 500 or a double-book.
2. **Admins can block lunch / PTO as exception rows** with
   `is_blocked=True`. Query is one `WHERE` clause; no special-case code.
3. **Reviewer experience.** `make seed` populates a real calendar
   (3 providers × 14 days × 14 slots/day). Reviewer talks to the bot and
   sees concrete options instead of "I have nothing on the 14th".

Trade-off: a real production EHR would have working-hours + exception
arithmetic; here we materialise the join. Documented in SOLUTION.md
"Architecture decisions" row 4.

---

## Bonus quick-fire

- **Why SQLite?** Zero-config, file-backed, ACID, reproducible. SQLAlchemy
  makes a Postgres swap a one-line connection-string change.
- **Why `Result[Ok, Err]` over `dict | None`?** Eliminates the
  `is the empty dict success or failure?` ambiguity at the boundary. `Err.code`
  is the public eval contract — scenarios assert against it.
- **Why long `CLINIC_PERSONA` (~1400 tokens)?** Crosses OpenAI's 1024-token
  prompt-cache threshold. Per-turn input cost drops measurably from the
  second turn onward.
- **Why no `Idempotency-Key` header?** `slot_id` IS the natural idempotency
  key. Same patient + same slot → return the existing appointment (200).
  Different patient → 409.
- **Why ElevenLabs Flash v2.5 specifically?** ~75 ms first-audio vs ~200 ms
  for the default constructor. One-line change, no behavioural delta.
