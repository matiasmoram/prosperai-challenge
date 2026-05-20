# Prosper Health voice-agent submission — senior-reviewer audit

Audit date: 2026-05-20.
Scope: the candidate is about to submit. Goal is to surface issues a senior
reviewer at Prosper would flag in interview, plus the production voice-agent
gotchas the candidate may not have covered.

---

## 1. README compliance gaps

Spec lists 5 named endpoints (`create_patient`, `find_patient`,
`list_availability_slots`, `create_appointment`, `cancel_appointment`).
We exposed them as REST resources (`POST /patients`, `GET /patients/by-phone`,
`GET /patients/by-name-dob`, `GET /availability`, `POST /appointments`,
`POST /appointments/{id}/cancel`).

The mapping is defensible — the brief explicitly says "the shape of the
request/response is up to you — design it the way you'd want a real
integration to look" — and REST resources read more like a real EHR than
RPC-style function endpoints. The bigger risk is the *expansion*: the spec
asks for one `find_patient(name, dob)`, we ship `by-phone` AND `by-name-dob`.
That is justifiable (phone is the fast voice-friendly path) and we mention
it in SOLUTION.md "Dev-log", but a pedantic reviewer will still ask:
"Where's `find_patient(name, dob)`? — that was the literal requirement."

**Verdict:** keep REST shape. Add one sentence in SOLUTION.md mapping the
spec names 1:1 to our routes, e.g. an "Endpoint mapping" table. Cost: 5 min.
Adding literal-named alias routes (`POST /find_patient` etc.) is not worth
it — they make the API surface inconsistent.

## 2. Edge cases not in scenarios

The six scripted scenarios cover the happy/edge minimum. Conspicuously
absent:

- **Two patients on same DOB, no phone match.** `find_patient_by_name_dob`
  returns multiple rows, but the dispatcher only auto-binds
  `identified_patient` when `len(patients) == 1`. There is no scenario
  asserting the bot disambiguates correctly. Likely failure: the LLM
  invents a choice, or the FSM stalls in `IDENTIFY_PATIENT` forever.
- **Slot starts in the past.** `list_available_slots` filters by date only
  (`Slot.start_at >= day_start` where day_start is `time.min` UTC) — it
  *does not* exclude slots in the past for today's date. If the caller
  says "today" at 4pm, the bot can offer (and successfully book) a 9am
  slot that has already happened.
- **Timezone confusion.** Persona prompt asserts US Eastern, but
  `_seed_provider_and_slots` writes timezone=`"UTC"` and slot timestamps
  are stored UTC. `list_availability_slots` window is `time.min ... +1day`
  in UTC. A 9am Eastern caller in winter (UTC-5) asking for "tomorrow 9am"
  could yield slots that the bot reads back as "9am" but are actually 9am
  UTC = 4am Eastern. No scenario detects this.
- **After-hours calls** — bot has no concept of clinic hours. It will
  cheerfully take a 3am call and book.
- **Diacritics in name** — `normalize_name` strips them
  (`"José" → "jose"`), which is reasonable for matching but means a
  registered "José" gets read back as "Jose" by TTS. Not tested.
- **Phone collision on `create_patient`** — `Patient.phone` is `unique`.
  If the caller is "new" but gives a phone that already exists, EHR
  returns 409 → `patient_exists`. The persona doesn't tell the bot what
  to do, and no scenario covers it.

**Top recommendation:** add a `slot_starts_in_past` scenario and a
`name_dob_two_matches` scenario. Each is ~25 lines in `evals/scenarios.py`.

## 3. Voice UX issues

- **slot_id / patient_id leakage.** `_record_tool_result` puts the full
  tool result (including UUIDs) into history via `str(result.value)`. The
  persona prompt says "never spell out database identifiers" but nothing
  *structurally* prevents the LLM from reading a UUID aloud. With
  `gpt-4o-mini` and a long conversation, this fails eventually.
  **Fix:** in `_record_tool_result`, strip `slot_id`/`patient_id`/
  `appointment_id` from what goes into history (keep them in `memory`
  for the dispatcher to inject back into the tool call). 15 lines.
- **Date pronunciation** — persona says "Tuesday, May 26th" not
  "2026-05-26". Good. But ElevenLabs TTS will read raw ISO strings
  literally if the LLM ever passes one through. Defense in depth: format
  dates server-side before they reach the LLM as text, not just trust the
  prompt.
- **"tomorrow" handling** is delegated to `_parse_dob` (dateutil), which
  does *not* understand relative phrases ("tomorrow", "next Tuesday").
  `dateparser` would; we explicitly dropped it. So if the LLM passes the
  literal word "tomorrow" as the `date` argument, parsing fails with
  `date_unparseable`. The persona pushes the LLM to convert
  ("Parse it tolerantly") but this is prompt-fragile.
- **Year pronunciation** — "2026" can become "twenty twenty-six" or "two
  thousand twenty-six" depending on TTS voice; we don't control it.
- **Phone read-back** — bot is told to confirm phone, but a 10-digit
  phone read by TTS without delimiters is a UX failure. Persona should
  instruct grouping (3-3-4 for US).

## 4. Tool-calling safety

The dispatcher's whitelist prevents the LLM from calling a tool *in the
wrong state*, but it does **not** prevent the LLM from passing a
hallucinated `slot_id` or `patient_id` to a tool that *is* whitelisted.

What happens today:
- Hallucinated `patient_id` → EHR returns 404 → `create_appointment_handler`
  maps to `Err(code="patient_or_slot_not_found", retryable=False)`. The
  LLM sees the error and can recover. Good.
- Hallucinated `slot_id` → same path → same error. Good.
- Reusing a stale `slot_id` from a previous `list_availability_slots`
  call that the caller actually rejected → succeeds silently if the slot
  is still free. The bot would book a slot the caller never agreed to.

**Defense:** in `_execute_tool`, when handling `create_appointment`,
validate `slot_id ∈ {s["slot_id"] for s in memory.last_slots}` and
`patient_id == memory.identified_patient["id"]`. Reject with a clear Err.
The `__use_first_slot__` test-only shortcut already proves the dispatcher
can do this kind of memory binding. ~20 lines, big safety win, worth
mentioning in the interview as a deliberate next step.

## 5. Concurrency

`data/ehr.db` is a single SQLite file with `check_same_thread=False`.
FastAPI runs requests on a thread pool. Realistic concurrency scenario:

- Two browser sessions, both in `BOOK_FLOW`, both call
  `list_availability_slots` and both see slot X free.
- Both reach `CONFIRM_BOOK`, both call `create_appointment(slot_id=X)`
  near-simultaneously.

What protects us: the **partial unique index** `uq_appointment_active_slot`
(`slots.models.Appointment.__table_args__`). SQLite will reject the second
INSERT with `IntegrityError`. But `repo.create_appointment` does not catch
`IntegrityError` — the `select(Appointment).where(...)` *first* and INSERT
*second* pattern has a TOCTOU window, and the second writer hits
`IntegrityError` which surfaces as a 500 from FastAPI, not the 409 with
`slot_taken` code the LLM knows how to handle.

**Verdict:** correctness is preserved (no double-booking) but the UX path
is wrong for one of the two concurrent callers. Fix in `repo.create_appointment`:
wrap the INSERT in try/except `IntegrityError`, re-query the holder, raise
`SlotTakenError`. ~10 lines. Mention SQLite WAL mode and a real DB engine
upgrade path as future work.

## 6. Observability gaps

Currently: `TimingCollector` emits one JSON-ish line per span (`{evt:"span",
phase, state, duration_ms}`) plus a session-end p50/p95 table. No Sentry,
no OTel, no `request_id` propagation between bot and EHR, no structured
logger config — `loguru` is imported but defaults are used.

For this challenge: that's fine. For SOLUTION.md "Future work" the right
framing is concrete and minimal:

1. Generate `session_id` per call, propagate as `X-Prosper-Session-Id`
   header from `EHRClient`, log on every EHR request.
2. Wrap timing spans in OTel-compatible structure (`trace_id`, `span_id`,
   `parent_span_id`). The shape is already 80% there.
3. Sentry for crash reporting on the bot pipeline only.
4. Per-state TTFT histogram (we already record `ttft` once per turn).

Each is a small PR. Calling these out by name in SOLUTION.md signals
operational maturity without committing to building them.

## 7. Security

The EHR has zero auth. Anyone with localhost can `curl POST /appointments`
and book on anyone's behalf. For a hiring challenge this is fine and
SOLUTION.md acknowledges it ("No auth / HIPAA encryption").

For the "Future work" section, sharpen the wording. The current entry is
generic. Better:

- **Service-to-service auth** between bot and EHR — shared HMAC or short-
  lived JWT in `X-Prosper-Bot-Token`, validated by FastAPI dependency.
  10 lines.
- **PHI at rest** — SQLite is dev only; move to Postgres + column-level
  encryption for `dob`, `phone`, `email`. Use a managed KMS for keys.
- **Audit log** — append-only table of every state-changing call
  (`create_patient`, `create_appointment`, `cancel_appointment`) with
  who/when/what. HIPAA Security Rule requirement, not optional in prod.
- **Caller authentication** — DOB-based identity verification is weak.
  Production would add a one-time SMS code before any cancel.
- **Rate limiting** — `slowapi` or similar to prevent enumeration attacks
  on `find_patient_by_phone`.

`.env` is gitignored; `env.example` should not contain real keys (verify).

## 8. The persona prompt — 3 targeted improvements

Current `CLINIC_PERSONA` is solid (~1100 tokens, written for cache-stability)
but it has gaps:

1. **No guidance for multi-match disambiguation.** When
   `find_patient_by_name_dob` returns 2+ rows, the bot has no script.
   Add: "If a patient lookup returns more than one match, do not guess —
   confirm a third detail (phone last 4 digits, email) before binding."
2. **No script for "I called the wrong number / wrong clinic".** Bot will
   try to identify the caller anyway. Add: "If the caller indicates they
   meant a different clinic or want a non-clinical service (pharmacy,
   prescription refill, lab), politely end the call: 'You've reached
   Prosper Health scheduling — I can only help with visits. Have a good
   day.'"
3. **Conflicting instruction on tool failure.** Current text says "retry
   once with corrected inputs OR escalate by offering to transfer." That
   "OR" gives the LLM permission to retry forever in practice. Tighten:
   "On the FIRST tool error, retry once with corrected inputs. On the
   SECOND, stop retrying and offer to take a message." Also: the bot has
   no `transfer_to_human` tool — promising one is a lie.

Other minor edits worth doing: explicit instruction to read phone numbers
in 3-3-4 grouping; explicit "do not confirm a booking before
`create_appointment` returns OK" (the LLM sometimes pre-celebrates).

## 9. 10 likely interview questions

1. "Why a custom dispatcher and not Pipecat Flows?" — answer ready in
   SOLUTION.md.
2. "Walk me through what happens when the LLM hallucinates a `slot_id`."
   (See §4. Candidate should know the current Err path AND admit the
   stale-slot reuse hole.)
3. "Two callers book the same slot at the same time — what happens?"
   (See §5.)
4. "How would you add a 'reschedule' intent without expanding the FSM
   to N states?" — likely answer: cancel-and-rebook reuses existing
   states, only the persona changes. SOLUTION.md hints this.
5. "Your eval pipeline calls OpenAI for both the bot AND the persona AND
   the judge — that's 3x cost per scenario. How do you keep this
   tractable in CI?" — answer: only `make eval` (manual / nightly), unit
   tests stay free.
6. "What's the smallest change that would let you swap out OpenAI for
   Claude or Gemini?" — `OpenAILLMAdapter` is one file, `LLMClientProtocol`
   is the contract.
7. "Show me how the per-state tool whitelist is enforced in code, line
   by line." — `dispatcher.py:124-137`.
8. "Why do tool handlers return `Result[Ok, Err]` instead of raising?"
   — typed branchability for the dispatcher AND the eval. Be ready to
   defend vs the pythonic "raise + try/except" alternative.
9. "How do you test latency without running real ElevenLabs?" — answer:
   in-process `httpx.ASGITransport`, `TimingCollector`, eval `--json`
   output. Honest answer: we don't test end-to-end audio latency at all.
10. "If I gave you 2 more days, what's the single biggest improvement?"
    — be ready with a concrete answer (streaming TTS or proactive
    prefetch). Don't say "more tests".

## 10. Quick wins (< 30 min each)

| Win | File | Time | Impact |
|---|---|---|---|
| Endpoint-mapping table in SOLUTION.md (spec name → our route) | `SOLUTION.md` | 5 min | Defuses §1 nitpick |
| Past-slot filter in `list_available_slots` | `src/prosper/ehr/repository.py:105` | 10 min | Closes obvious bug |
| Catch `IntegrityError` in `create_appointment`, raise `SlotTakenError` | `src/prosper/ehr/repository.py:134` | 15 min | Correct concurrency UX |
| Strip UUIDs from tool-result strings written to history | `src/prosper/dispatcher.py:160-178` | 20 min | Voice safety |
| `slot_id` / `patient_id` validation against `memory` before `create_appointment` | `src/prosper/dispatcher.py:148` | 25 min | Hallucination defense |
| Add `slot_starts_in_past` + `name_dob_two_matches` scenarios | `evals/scenarios.py` | 25 min | Coverage |
| Tighten persona: disambiguation, off-clinic, retry-budget | `src/prosper/prompts.py` | 20 min | UX correctness |
| Paste an actual sample transcript into SOLUTION.md placeholders | `SOLUTION.md:143,151` | 15 min | Looks unfinished otherwise |
| Add 1-line `make ehr` reminder that EHR must be running before `make eval` (or auto-spawn) | `Makefile` | 10 min | Onboarding friction |
| Sharpen "Security" entry in "Future work" with the 5 bullets in §7 | `SOLUTION.md:181` | 10 min | Signals seniority |

Total: under 3 hours to ship every item. The first five are bug fixes;
the last five are documentation polish. All ten meaningfully change a
senior reviewer's read of the submission.
