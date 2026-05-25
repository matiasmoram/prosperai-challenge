# Open questions for Matías (autonomous-run adjudication)

> Created during the unsupervised completion run (2026-05-25). The lead drove
> the full backlog (F6 + Waves 5–8) without stopping, per "acaba todo / no
> pares". For every decision that would normally need your input, the lead
> made a **reasonable interim choice to keep moving** and logged it here. When
> you return: review each, confirm or correct. Nothing here is destructive —
> all are additive choices or deferred items, reversible on the branch.

Format: `Q — context · INTERIM CHOICE made · how to change it`.

---

## Decisions taken as interim defaults

(appended as the run proceeds)

- **Q1 — Duration clinical floor enforcement.** The triage `minimum_safe_minutes`
  is now a code contract (`Err below_minimum_safe_duration`) per the LLM-council
  verdict + audit F-002, not just a prompt instruction. INTERIM CHOICE: enforce
  in the dispatcher (reject sub-floor bookings, LLM re-offers ≥ floor). Change:
  if you'd rather keep it advisory-only, revert the guard in
  `dispatcher._validate_against_memory`.

---

## Needs LIVE verification (can't reproduce offline)

- **Barge-in / voice overlap (req 7).** You reported it "mal handleado". The
  investigation found the infrastructure already fully wired + correct
  (TTSAudibleObserver → `mark_last_assistant_interrupted` truncates the assistant
  history to what was actually spoken → `turn_interrupted` event; VAD tuned;
  persona annotated). Added 6 unit tests for the truncation logic. BUT the
  real-time barge-in path (VAD → InterruptionFrame → observer) cannot run in CI
  (no audio). INTERIM: shipped as-is + unit-tested. ACTION FOR YOU: call the bot,
  interrupt mid-sentence, and report what specifically feels wrong (bot keeps
  talking? repeats the cut-off line? ignores you?) — that symptom is needed to fix
  any real runtime issue, since it can't be reproduced offline.

## Architectural-authority questions (review mode, 2026-05-25)

- **Two-tier intent routing in CHOOSE_INTENT (regex fast-path + LLM `route_intent`
  fallback).** A reviewer flagged that the regex in `_maybe_transition_from_user_text`
  runs BEFORE the LLM turn, so it front-runs the `route_intent` tool: any decisive
  phrasing the regex matches transitions without an LLM round-trip, and `route_intent`
  only resolves the phrases the regex misses. INTERIM CHOICE: **keep both** — the regex
  is a deliberate latency optimization (hard rule 7) and routes the key cases correctly
  (reschedule-first; "change my appointment" → reschedule). Documented the two-tier
  contract in-code so nobody collapses it by accident. FSM edge-validation prevents
  double-fire. CHANGE IT IF: you'd rather have a single source of truth and let the LLM
  own ALL CHOOSE_INTENT routing (simpler, but +1 LLM round-trip per call → slower). This
  is an architecture-authority call, hence logged not unilaterally refactored.

## Deferred / flagged (not done — your call)

- **route_intent ambiguity (F7 sim finding).** An ambiguous "I want to change my
  appointment" sometimes routes to CANCEL_FLOW (cancel-then-rebook) instead of
  RESCHEDULE_FLOW. Not a hallucination; a phrasing-classification weakness.
  NOT addressed (would be an intent-classifier tweak, product-ish). Decide
  whether to harden route_intent's reschedule-vs-cancel disambiguation.
