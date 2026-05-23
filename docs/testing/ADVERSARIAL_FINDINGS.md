# Adversarial findings log

Running log of bugs surfaced by adversarial / fuzz-style testing. Each entry
is pinned by a test under `tests/adversarial/`. Confirmed bugs are pinned with
`@pytest.mark.xfail(strict=True)` so the suite stays green **and** the day
someone fixes the bug the test XPASSes and forces removal of the marker (a
nudge to close the loop). Known-acceptable gaps are noted as such.

Severity: 🔴 high (data integrity / PII leak / wrong booking) · 🟠 medium ·
🟡 low (edge / cosmetic).

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
