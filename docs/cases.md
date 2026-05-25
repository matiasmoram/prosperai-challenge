# cases.md — what the tests cover

A catalogue of the **cases** exercised by the test + eval suites, drawn from the
whole history of the branch (40 commits, `main..feat/hybrid-llm-navigation`). For
the *machinery* that runs these (AI vs non-AI, how to invoke), see `tester.md`.
For *what the bot does*, see `FEATURES.md`.

Headline counts (today):
- **712 pytest tests** collected (`tests/` + `tester/` + the typed eval checks), 1 skipped (live).
- **107 mock-eval scenarios** (deterministic, offline, paired state-assertion + LLM-judge contract).
- **13 adversarial findings (F-001…F-013) — all closed at root cause**, each with regression tests.
- **Live persona simulator** + **messy-human / ASR-noise** suite (AI-driven, gated on a key).

---

## 1. Happy paths (mock-eval, tag `happy` ×27)

- New patient: identify-by-phone miss → register → triage → book → confirm → goodbye.
- Existing patient: identify-by-phone → book / cancel / reschedule → confirm.
- Cancel one of several appointments (pick by ordinal from a numbered read-back).
- Book with notes; book 60-min (2 slots) and 90-min (3 slots) durations.

## 2. Identity & registration (tags `identity` ×9, `disambiguation` ×5, `registration` ×3)

- Find by phone; fall back to name+DOB.
- Name+DOB returns multiple candidates sharing a DOB → numbered read-back →
  resolve "the first one" / "number two" / "two".
- Partial name then correction mid-turn.
- Not-found-existing → front-desk handoff; genuinely-new → register.
- Identity gate: booking tools rejected before identity is set (`tool_rejected`).

## 3. Booking, triage & duration (tags `triage` ×8, `duration` ×8, `specialty` ×6)

- Symptom → specialty routing (GP / therapist / psychiatry / derm / physio).
- Ambiguous symptom → low-confidence → follow-up question.
- Direct specialty request skips triage.
- Duration **soft-override** (caller picks ≥ floor), **extend accepted** (longer ok),
  **sub-floor refused** (clinical floor communicated, books at floor).
- Specialty fully booked → propose alternative; specialty changed mid-flow.
- Triage clamp (floor > recommended silently lowered) — unit-tested in `test_triage.py`.

## 4. Cancel & reschedule (tags `cancel` ×8, `reschedule` ×14)

- Cancel with read-back + confirm; cancel the Nth of several.
- Atomic reschedule; reschedule when the requested day is fully booked.
- Reschedule picks the right appointment by provider / ordinal.
- Cancel-then-rebook chain fires **only** on a mid-cancel intent flip (not for "reschedule").

## 5. Intent routing (tags `hybrid` ×4, `routing`/`intent_routing` ×4)

- `route_intent` resolves to book / cancel / reschedule; unknown → clarify.
- "change my appointment" → reschedule (not cancel); pinpoint by provider.
- Regex fast-path vs LLM fallback two-tier coverage.

## 6. Confirm-state & abandon (tags `recovery` ×19, `abandon` ×9, `deny` ×2)

- "No / different time" at a confirm state routes back to the flow to re-offer.
- Stop-word / goodbye mid-flow aborts cleanly.
- Confirm-then-abort-then-re-pick.
- Goodbye at each confirm state (book/cancel/reschedule).

## 7. Handoff & reliability (tags `handoff` ×3)

- Caller requests a human → handoff mail → HANDOFF → confirmation.
- Bot stuck (loop exhaustion) → `bot_failed` mail → graceful end.
- LLM total failure → canned line + reception mail (`test_llm_failure.py`).

## 8. Adversarial findings — all closed, all regression-tested (`tests/adversarial/`)

| ID | The case caught | Regression test |
|----|-----------------|-----------------|
| F-001 | `_parse_dob` silently completed partial/garbage dates to *today* | `test_parse_dob_defaults.py` |
| F-002 | sub-floor duration booking not code-enforced | `test_dispatcher_gaps.py` |
| F-003 | `redact_pii` missed 7-digit + hex-glued phone runs | `test_redact_gaps.py` |
| F-004 | `mask_name` of all-single-letter names yielded no mask char | `test_redact.py` |
| F-005 | same-patient re-booking mislabelled "taken by another" | `test_ehr_self_collision.py` |
| F-006 | CHOOSE_INTENT misrouted booking phrases w/ a cancel-ish verb | `test_choose_intent_routing.py` |
| F-007 | raw caller PII written to the durable audit log | `test_audit_pii.py` |
| F-008 | EHR transport failure (down/timeout) crashed the turn | `test_ehr_transport_errors.py` |
| F-009 | "confirm + goodbye" in one breath dropped the action | `test_confirm_goodbye.py` |
| F-010 | global console bus evicted a quiet session's events | `test_bus_cross_session.py` |
| F-011 | medical-emergency red flag was advisory, not enforced | `test_emergency_not_enforced.py` |
| F-012 | mid-utterance "bye" hung up (homophone of "by the way") | `test_goodbye_false_positive.py` |
| F-013 | pronoun "one" mis-identified caller during disambiguation | `test_identity_disambiguation.py` |

Plus standing adversarial guards: hostile-LLM dispatcher (`test_hostile_llm_dispatcher.py`),
fuzz invariants (`test_fuzz_invariants.py`), EHR invariants (`test_ehr_invariants.py`),
audit-replay robustness, handle-index parsing, phone normaliser, cancel-rebook chain,
and eval-harness blind-spots (the tests that test the tests).

## 9. Garbled / messy-human cases (AI sim — `tester/`)

- **Disfluency**: fillers, repetitions, false starts (`tester/noise.py`).
- **ASR mishearings**: homophones, number-word swaps (fifteen↔fifty), the dangerous
  intent flip (cancel↔schedule, intermittent), dropped day-numbers, phone format drift.
- **Contract asserted** (`tester/clarification.py`): a write that lands on a garbled
  turn with no clarification/confirm is a `plowed_ahead_on_garble` violation.
- 4 MESSY live personas (garbled numbers, intent-reversal trap, disfluent mind-changer,
  dropped day) — last live run: **0 plowed-ahead** (bot fails safe under garble).
- Hallucination-bait + prompt-injection personas: bot refuses to fake a confirmation.

## 10. Backend & infra cases (`tests/ehr/` ×85, `tests/console/` ×51, `tests/integrations/` ×16)

- EHR: availability (past-slots filtered), one-active-appointment-per-slot 409 race,
  atomic reschedule rollback, schema max-length caps, calendar range query, fuzzy bands.
- Console: 9 typed events, per-session bus isolation, audit-write redaction, SSE replay
  idempotency, masked-field validation.
- Integrations: MailStore round-trip + PII + newest-first + path-traversal guard; `/frontdesk` router.

---

> Coverage is **paired** at the eval layer: a mock-eval scenario passes only if the
> FSM state assertion (real DB delta + terminal state + tool sequence) **and** the LLM
> judge both pass (ADR 003). Judge-only passes are treated as false positives.
