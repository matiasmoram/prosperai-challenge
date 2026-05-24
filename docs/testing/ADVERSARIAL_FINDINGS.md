# Adversarial findings log

> **STATUS (resolved): all of F-001 … F-013 are FIXED at the root cause.**
> The `tests/adversarial/` suite now passes as plain assertions of the
> corrected behaviour. Full repo gate green: ruff + `ruff format` +
> `mypy --strict` (27 files) + **547 passed / 1 skipped**, and `make mock-eval`
> (56/56 `state=P judge=P`) — re-verified 2026-05-24. Each finding's "Suggested
> fix" below is what was implemented; the **Fix** column in the summary table
> records the status. M-001's F-008/F-009 classes are now covered by direct
> unit tests in the adversarial suite.
>
> **⚠ Read the per-finding "Suggested fix (needs sign-off)" lines as historical
> proposals, NOT open work.** They are the original write-ups from when each bug
> was found; the summary table's "Fix (all ✅ implemented)" column is the current
> status. Every F-NNN is CLOSED and pinned by a passing test — do not
> re-implement a fix for any of them.

Running log of bugs surfaced by adversarial / fuzz-style testing. Each entry
is pinned by a test under `tests/adversarial/`. Bugs were originally pinned
with `@pytest.mark.xfail(strict=True)` (so the suite stayed green and a fix
would XPASS and force marker removal); after the fix the markers were removed
and the tests assert the corrected behaviour. Known-acceptable gaps are noted.

Severity: 🔴 high (data integrity / PII leak / wrong booking) · 🟠 medium ·
🟡 low (edge / cosmetic).

## Summary

| ID | Sev | One-liner | Fix (all ✅ implemented) |
|----|-----|-----------|-----|
| F-001 | 🔴 | `_parse_dob` completes partial dates to *today* | double-default reject (`tools.py`) |
| F-002 | 🔴 | `_phone_words_to_digits` injects a `4` from filler "for" | neighbour-gated "for" (`tools.py`) |
| F-003 | 🟠 | `redact_pii` misses hex-glued phone numbers | digit (not hex) boundary (`redact.py`) |
| F-004 | 🟡 | `mask_name("A B")` → no mask char → drops event | `max(1, len-1)` (`redact.py`) |
| F-005 | 🟠 | same-patient re-book reported as "taken by another" | extend-in-place (`repository.py`) |
| F-006 | 🟠 | CHOOSE_INTENT misroutes "skip/move … book" to cancel | strong-verb tiebreak (`dispatcher.py`) |
| F-007 | 🔴 | raw caller PII in `transcript_turn` → durable audit | redact on audit write (`audit.py`) |
| F-008 | 🔴 | EHR transport failure crashes the turn (uncaught httpx) | wrap → `EHRHTTPError(503)` (`ehr_client.py`) |
| F-009 | 🔴 | "yes book it, bye" at CONFIRM → END; action lost | affirm-wins-at-CONFIRM (`dispatcher.py`) |
| F-010 | 🟠 | global bus evicts a quiet session's events under load | per-session subscribe (`bus.py`/`sse.py`) |
| F-011 | 🟠 | medical-emergency red flag advisory, not enforced | FSM `medical_emergency`→END (`flows.py`) |
| F-012 | 🟠 | mid-utterance "bye" (STT homophone) hangs up | bare "bye" trailing-only (`dispatcher.py`) |
| F-013 | 🔴 | pronoun "one" ("one more time", "not that one") mis-IDs caller as candidate #1 | standalone/cued cardinal only (`dispatcher.py`) |
| M-001 | 🟠 | eval suite blind to F-008/F-009 classes | now covered by adversarial unit tests |
| N-001..4 | ✅ | verified NON-bugs (reschedule rollback, tz, observer, fuzz, handle off-by-one) | n/a |
| O-001 | ⚠️ | pre-existing `test_sse.py` failure (concurrent agent) | resolved by that agent; untouched |

---

## F-001 🔴 `_parse_dob` silently completes partial/garbage dates to *today*

- **Where:** `src/prosper/tools.py` `_parse_dob`
- **Repro:**
  - `_parse_dob("March")` → `Ok(date(2026, 3, 23))`  (today's day-of-month injected)
  - `_parse_dob("3pm")`   → `Ok(date(2026, 5, 23))`  (today — no date at all in input)
  - `_parse_dob("15")`    → `Ok(date(2026, 5, 15))`  (today's month + year injected)
  - `_parse_dob("May")`   → `Ok(date(2026, 5, 23))`
- **Root cause:** `dateutil.parser.parse(raw, fuzzy=False)` fills any missing
  date component from `datetime.now()`. `fuzzy=False` only stops it from
  *ignoring* junk tokens; it does **not** stop default-injection. The docstring
  claims "ambiguous input should fail loudly so the LLM re-asks the user" — it
  does not.
- **Impact:** A caller DOB of "May" registers a patient with `dob = today`.
  A name+DOB lookup with a partial DOB silently searches the wrong date. This
  is EHR data corruption / mis-identification, not a cosmetic slip.
- **Suggested fix (needs sign-off — touches a contract):** pass an explicit
  sentinel `default` to `dateutil` and reject the parse if any of
  year/month/day was filled from the default; or require a 3-component date.
- **Pinned by:** `tests/adversarial/test_parse_dob_defaults.py`

## F-002 🔴 `_phone_words_to_digits` injects a spurious `4` from filler "for"

- **Where:** `src/prosper/tools.py` `_phone_words_to_digits` / `_NUMBER_WORDS`
- **Repro:**
  - `_phone_words_to_digits("for 5551234567")` → `"45551234567"` (leading 4 added)
  - `_phone_words_to_digits("555 for 1234567")` → `"55541234567"` (4 spliced mid-number)
- **Root cause:** `_NUMBER_WORDS` maps `"for" → "4"` to recover the STT slip
  "five-for-six" → 456. But "for" is also an extremely common English filler.
  When it appears as a real word adjacent to digits, it corrupts an otherwise
  valid phone number into a different one.
- **Impact:** A corrupted phone either fails the patient lookup (caller told
  they're not in the system) or, on `create_patient`, persists a wrong number
  to the EHR.
- **Suggested fix (needs sign-off):** only map `"for" → "4"` when it sits
  *between* two number-words, never adjacent to a literal digit run; or drop
  the `"for"` alias and rely on the LLM normaliser.
- **Pinned by:** `tests/adversarial/test_phone_normaliser.py`

## F-003 🟠 `redact_pii` misses 7-digit local numbers and hex-glued runs

- **Where:** `src/prosper/observability/redact.py`
- **Repro:**
  - `redact_pii("call me at 555-0142")` → unchanged (7 digits < the 10-digit floor)
  - `redact_pii("ref2025550142 today")` → unchanged (run preceded by hex letter `f`)
- **Root cause:** `_looks_like_phone` requires 10–15 digits; the regex's
  `(?<![0-9A-Fa-f])` lookbehind (added to skip UUID interiors) also skips any
  phone glued to a word ending in `a–f`.
- **Impact:** PHI (a phone number) reaches `journalctl` unmasked. The 10-digit
  case is the common one and IS handled; these are the tail gaps.
- **Status:** the 7-digit floor is documented in the module docstring as a
  deliberate limitation; the hex-glued miss is an unintended false-negative.
  Pinned as `xfail` (hex case) + plain assertion (documents 7-digit behaviour).
- **Pinned by:** `tests/adversarial/test_redact_gaps.py`

## F-004 🟡 `mask_name` of all-single-letter names yields no mask char

- **Where:** `src/prosper/observability/redact.py` `mask_name`
- **Repro:** `mask_name("A B")` → `"A B"` (no `*`)
- **Impact:** `Dispatcher._publish_patient_identified` builds
  `name_masked="A B"`, which `events.validate_event` rejects ("no masking
  character"); `_publish` swallows the `ValueError` and the
  `patient_identified` operator-console event is silently dropped for that
  caller. No crash, but a telemetry hole.
- **Pinned by:** `tests/adversarial/test_redact_gaps.py`

## F-005 🟠 Same-patient re-booking reported as "taken by another patient"

- **Where:** `src/prosper/ehr/repository.py` `create_appointment`; misleading
  label applied in `src/prosper/tools.py` (`slot_taken_other_patient`).
- **Repro:** patient books anchor slot S for 30 min, then re-books S for
  60 min (adjacent block free) → `SlotTakenError(owner_patient_id=<self>)`.
- **Root cause:** the idempotency fast-path requires BOTH `patient_id` AND
  `duration_minutes` to match; any duration change for the same patient on the
  same anchor falls through to the generic "slot taken" branch with
  `owner = existing.patient_id` (which is the caller). `tools.py` then maps
  the 409 to `slot_taken_other_patient` unconditionally, so the bot tells the
  caller their own slot belongs to someone else.
- **Impact:** confusing-but-not-destructive UX; the caller cannot extend
  their own appointment and is misinformed about why.
- **Suggested fix (needs sign-off):** when `existing.patient_id == patient_id`,
  treat as a re-book/extend (validate the longer chain and update duration)
  or raise a distinct `own_appointment_exists` error; and have `tools.py`
  compare `owner_patient_id` to the identified patient before labelling.
- **Pinned by:** `tests/adversarial/test_ehr_self_collision.py`

## F-006 🟠 CHOOSE_INTENT misroutes booking phrases with a cancel-ish verb

- **Where:** `src/prosper/dispatcher.py` `_maybe_transition_from_user_text`
- **Repro (state=CHOOSE_INTENT):**
  - "let's skip the chit-chat, I want to book an appointment" → CANCEL_FLOW
  - "I want to move forward with booking a visit" → CANCEL_FLOW
  - "remove all this confusion and just schedule me" → CANCEL_FLOW
- **Root cause:** `_CANCEL_INTENT` (broadened to `skip|drop\w*|move\w*|`
  `remove\w*|delete\w*|…`) is tested **before** `_BOOK_INTENT`. Those verbs are
  also common in booking phrasings, so cancel wins even when an explicit
  booking word is present.
- **Impact:** caller wanting to book is sent to CANCEL_FLOW. New patient →
  "nothing to cancel" → END (call lost); existing patient → bot reads their
  appointments to cancel.
- **Suggested fix (needs sign-off):** when both `_BOOK_INTENT` and
  `_CANCEL_INTENT` match, disambiguate (prefer the intent whose keyword is
  closer to an object like "appointment", or require cancel verbs to be
  adjacent to "appointment/booking/visit").
- **Pinned by:** `tests/adversarial/test_choose_intent_routing.py`

## F-007 🔴 Raw caller PII in `transcript_turn` is written to the durable audit log

- **Where:** `src/prosper/console/events.py` (validation only checks `*_masked`
  fields) + `src/prosper/console/audit.py` `AuditJSONLWriter.write` (persists
  every event verbatim).
- **Repro:** publish a `transcript_turn` with
  `payload={"role":"user","text":"my number is 202-555-0142","turn_id":1}`;
  the string `202-555-0142` appears verbatim in
  `data/audit/<session>.jsonl`.
- **Root cause / inconsistency:** tool arguments are scrubbed by
  `dispatcher._redact_tool_args` before the bus (phones masked, DOB/notes/
  reason/symptoms placeholder'd). Transcript text is **not** — `text` is not a
  `*_masked` key, so the bus-level safety net never inspects it, and the same
  text is the most likely place for spoken PII (phone, DOB, SSN).
- **Impact:** PII-at-rest. The live SSE showing raw text to the operator is
  documented as intentional; persisting it to a durable on-disk log is not,
  and contradicts the redaction effort applied everywhere else.
- **Decision needed (product/policy, not silently fixed):** either (a) accept
  and document that the audit log contains raw transcript PII (and protect the
  directory accordingly), or (b) apply `redact_pii` to `transcript_turn.text`
  on the audit-write path while leaving the live SSE raw.
- **Pinned by:** `tests/adversarial/test_audit_pii.py`

## F-008 🔴 EHR transport failures (down / timeout) crash the turn

- **Where:** `src/prosper/ehr_client.py` `_request` (only raises on HTTP
  status >= 400) + every handler in `src/prosper/tools.py` (catches only
  `EHRHTTPError`).
- **Repro:** point an `EHRClient` at a transport that raises
  `httpx.ConnectError` (EHR process down) and call any handler →
  `httpx.ConnectError` propagates **uncaught**. Same for `httpx.ReadTimeout`
  (slow EHR). `Dispatcher._execute_tool` / `_llm_turn` do not wrap the call,
  so it bubbles out of `handle_user_turn` and crashes the turn.
- **Contrast:** an HTTP 500 that actually *arrives* IS caught and becomes
  `Err(ehr_error)` — the gap is specifically transport-level errors.
- **Impact:** the most common real outage (EHR unreachable / slow) bypasses
  the entire `Result[Ok, Err]` + retry design and drops the call, instead of
  the bot saying "I'm having trouble reaching our system, one moment".
- **Suggested fix (needs sign-off — touches client contract):** in
  `_request`, wrap the `await self._c().request(...)` in
  `try/except httpx.HTTPError` (or `TransportError`/`TimeoutException`) and
  re-raise as `EHRHTTPError(status_code=503, detail=...)`, so the existing
  `except EHRHTTPError` in each handler converts it to `Err(ehr_error,
  retryable=True)`.
- **Pinned by:** `tests/adversarial/test_ehr_transport_errors.py`

## F-009 🔴 "Confirm + goodbye" in one breath drops the action at CONFIRM_*

- **Where:** `src/prosper/dispatcher.py` `_maybe_transition_from_user_text`
  (goodbye intent is checked first, from any state).
- **Repro (pure routing, no LLM):**
  - CONFIRM_BOOK + "yes, book it, thanks bye" → END
  - CONFIRM_CANCEL + "yes cancel it, goodbye" → END
  - CONFIRM_RESCHEDULE + "yes move it, bye" → END
  - (control) CONFIRM_BOOK + "yes book it" → stays CONFIRM_BOOK ✅
- **Root cause:** at a CONFIRM_* state the executing tool
  (create/cancel/reschedule_appointment) is only mounted while still in that
  state. A trailing goodbye routes to END (which has no tools), so the
  just-confirmed action never fires.
- **Impact:** 🔴 hallucinated success — caller says "yes, perfect, bye!",
  believes they booked/cancelled, EHR has nothing. Exactly the worst failure
  mode per CLAUDE.md hard-rule #5.
- **Suggested fix (needs sign-off):** at CONFIRM_* states, if the utterance
  also affirms, let the affirmation win (stay in CONFIRM so the action runs)
  and honour the goodbye on the following turn; or defer goodbye when a
  pending confirmation is unresolved.
- **Pinned by:** `tests/adversarial/test_confirm_goodbye.py`

## F-010 🟠 Global console bus can evict a quiet session's events under load

- **Where:** `src/prosper/console/bus.py` (one shared bounded queue per
  subscriber) + `src/prosper/console/sse.py` `_live_event_iter` (filters by
  `session_id` only *after* dequeue).
- **Repro (deterministic, `queue_depth=4`):** subscribe, publish 1 event for
  session "watched", then 4 for session "chatty"; draining the queue shows the
  "watched" event was evicted (drop-oldest) — its SSE consumer never sees it.
- **Root cause:** subscriber queues are global, not per-session. A subscriber
  watching a quiet session still buffers every other session's events in the
  same bounded queue; a burst from a chatty session evicts the quiet session's
  rare events.
- **Impact:** none for the 1-2 calls of the demo; under genuine multi-session
  load an operator watching session X can silently lose X's telemetry to
  unrelated traffic.
- **Suggested fix (needs sign-off):** key subscriptions by `session_id` (the
  subscriber registers interest in one session and the bus only enqueues
  matching events), or fan out per-session queues.
- **Pinned by:** `tests/adversarial/test_bus_cross_session.py`

## F-011 🟠 Medical-emergency red flag is advisory, not FSM-enforced

- **Where:** `src/prosper/tools.py` `suggest_specialty_handler` returns
  `Err(code="medical_emergency")` on a triage red flag; `dispatcher.py`
  `_maybe_transition_from_tool` has no `suggest_specialty` branch.
- **Repro:** in BOOK_FLOW, a `medical_emergency` Err triggers no transition —
  the state stays BOOK_FLOW with `list_availability_slots` mounted, and
  CONFIRM_BOOK still exposes `create_appointment`. The 911 redirect depends
  entirely on the LLM honouring the persona prompt.
- **Impact:** if the LLM ignores the flag (prompt drift, jailbreak, model
  swap), the bot can book a routine visit for a caller describing a
  life-threatening emergency. No hard guardrail.
- **Suggested fix (needs sign-off):** route a `medical_emergency` Err to a
  dedicated terminal/EMERGENCY state (or force END with an emergency outcome)
  so the booking tools are physically unmounted, independent of the LLM.
- **Pinned by:** `tests/adversarial/test_emergency_not_enforced.py`

## F-012 🟠 Mid-utterance "bye" hangs up the call (STT homophone of "by the way")

- **Where:** `src/prosper/dispatcher.py` `_GOODBYE_HARD` (matches `\bbye\b`
  anywhere) via `_has_goodbye_intent`, checked first from any state.
- **Repro:** at BOOK_FLOW, "bye the way, can you also book me Tuesday" → END;
  "the kids said bye to grandma, anyway book Tuesday" → END. Controls "by the
  way …" and "my standby appointment …" correctly stay in BOOK_FLOW.
- **Root cause:** tier-1 hard-goodbye matches the bare word "bye" anywhere in
  the utterance. ElevenLabs realtime STT frequently transcribes the ubiquitous
  "by the way" as "bye the way", so an in-progress task is abandoned.
- **Impact:** caller mid-booking is dropped to END and must start over;
  same over-eager-goodbye family as F-009.
- **Suggested fix (needs sign-off):** require bare "bye" to sit at an utterance
  boundary (mirror the tier-2 trailing anchor), keeping "goodbye"/"hang up"/
  "end the call" matchable anywhere.
- **Pinned by:** `tests/adversarial/test_goodbye_false_positive.py`

## F-013 🔴 Pronoun "one" mis-identifies the caller during name+DOB disambiguation

- **Where:** `src/prosper/dispatcher.py` `_pick_candidate_index` (the new
  fuzzy multi-candidate picker). Found while auditing the concurrent
  disambiguation feature.
- **Repro (count=2 candidates):** "can you repeat them one more time" → 0;
  "not that one" → 0; "which one was that" → 0; "neither one" → 0; "the fifth
  one" → 0. The bare word "one" matched `\b(one|two|three|four)\b` anywhere.
- **Impact:** 🔴 a caller who is unsure, asks to repeat, or rejects an option
  is silently identified as candidate **#1** and advanced to CHOOSE_INTENT
  under the wrong identity — an identity-checkpoint failure (PHI exposure /
  wrong-patient booking).
- **Fix:** the spoken cardinal is now accepted only when it is standalone
  ("two", "two please") or explicitly cued ("number two", "option one"); a
  cardinal embedded in a phrase is treated as a pronoun and ignored. Digits and
  ordinals are unchanged. Genuine picks ("one"/"two"/"number two") still work.
- **Pinned by:** `tests/adversarial/test_identity_disambiguation.py`

## M-001 🟠 Meta: the eval suite is blind to the F-008 and F-009 classes

- **F-009 blind spot:** `evals/runner.py` `run_scenario` calls
  `_is_persona_stop(user_text)` and, on a match, sets `dispatcher.state =
  State.END` and breaks **before** `handle_user_turn`. "yes, book it, thanks
  bye" / "yes cancel it, goodbye" all match the stop pattern, so the harness
  hangs up the call before the dispatcher processes the combined utterance —
  the F-009 routing path never runs in mock or live evals.
- **F-008 blind spot:** scenarios mount the EHR in-process via
  `httpx.ASGITransport` (no socket), so a transport-level failure
  (ConnectError / timeout) can never occur in a scenario run — the F-008 class
  is structurally unreachable by the suite.
- **Impact:** two of the highest-severity findings here would pass CI green.
  Worth a deliberately non-hermetic reliability test (point an `EHRClient` at a
  dead port) and a confirm+goodbye scenario that drives the utterance through
  `handle_user_turn` directly rather than the persona-stop short-circuit.
- **Pinned by:** `tests/adversarial/test_eval_harness_blindspots.py`

## O-001 ⚠️ Observation — pre-existing failing test from concurrent in-flight work (NOT touched)

- **Failing test:** `tests/console/test_sse.py::test_replay_endpoint_returns_events_in_order`
  (`KeyError: 'ts'`).
- **Root cause:** `sse._replay_event_iter` now emits a terminal SSE frame
  `event: replay_complete\ndata: {}\n\n`. The test helper
  `_parse_sse_data_frames` collects *every* `data:` line, so it parses the
  terminator's `{}` and then `f["ts"]` raises. The source (sse.py) was updated
  to add the terminator but the test's frame parser was not — a half-applied
  edit by a concurrent agent (sse.py / events.py / test_sse.py are all
  uncommitted `M`).
- **Action:** left untouched — this is another agent's active work; the fix is
  one line in `_parse_sse_data_frames` (skip frames whose preceding `event:`
  line is `replay_complete`, or ignore empty `{}` payloads). Surfaced here so
  it isn't mistaken for an adversarial-suite regression.

## N-001 ✅ NOT a bug (verified, kept as positive regression context)

- Reschedule failure preserves the original: `reschedule_appointment` deletes
  this appointment's locks with `flush()` (not commit) before re-resolving the
  new chain; on `NoConsecutiveSlotsError`/conflict the `session.close()` in the
  FastAPI session dep rolls back the uncommitted delete, so the original
  appointment and its locks survive. No orphan state.
- Naive/aware datetime mix in `repository.py` queries: harmless on SQLite —
  the dialect's DATETIME storage format ignores tzinfo and the query side uses
  UTC components, so naive-stored slots and aware-UTC query bounds compare
  correctly. (CLAUDE.md's warning is about naive-*local*, not aware-UTC.)

## N-004 ✅ Latent footgun guarded — handle "0" off-by-one

- `_maybe_handle_index("0")` returns `-1`; the resolution sites guard with
  `0 <= idx < len(...)` so "slot 0" is left unresolved → `hallucinated_slot_id`,
  never `last_slots[-1]`. Safe today, but fragile — pinned by
  `tests/adversarial/test_handle_index.py` so dropping the lower-bound check
  (which would silently book the WRONG, last slot) breaks loudly. Fuzzing 30k
  random handle strings found no crashes in `_maybe_handle_index`,
  `_parse_dob`, or `_phone_words_to_digits`.

## N-003 ✅ Robustness verified by seeded fuzz (20k inputs, 0 violations)

- `redact_pii` is idempotent and total; `mask_phone` never emits a 7+ digit
  run (always passes the event PII net); `normalize_phone` is idempotent;
  `normalize_name` never raises on arbitrary unicode. Pinned by
  `tests/adversarial/test_fuzz_invariants.py` (committed run: 3k inputs/seed).

## N-002 ✅ NOT a bug (verified) — observer fires on_interrupt when not speaking

- `TTSAudibleObserver` calls `on_interrupt("")` even with no active bot turn.
  This looked like it could falsely mark a completed turn as "[NOT HEARD]", but
  it is **intentional and tested** (`tests/test_observers.py::`
  `test_tts_text_ignored_when_not_speaking` asserts `captured == [""]`), and
  `Dispatcher.mark_last_assistant_interrupted` guards with
  `if last.get("role") != "assistant": return` — so once the next user turn is
  recorded the stray interrupt is a no-op. No false-mark in the normal flow.
