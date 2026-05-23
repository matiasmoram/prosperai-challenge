# Execution Plan — Three User-Reported Bugs (2026-05-23)

User reports three bugs/UX-defects in the voice agent. This plan kills all three. Research backing in `docs/research/interruption_design.md`, `docs/research/speculative_race.md`, plus inline notes for filler audit.

---

## Bugs in scope

| # | Bug | Recommended approach |
|---|---|---|
| A | Interrupt mid-bot-turn: dispatcher history claims bot fully spoke; LLM next turn is incoherent. User wants timeline-aware history (LLM sees full bilateral timeline incl. interrupted partial line + marker). | Design B from `interruption_design.md`: `TTSAudibleObserver` processor between TTS + transport.output. ~80-120 LoC. |
| B | Greeting is generic + sequential. Bot needs branded warm opener and start patient lookup ASAP while caller talks. | New first line: *"Hi, you've reached Prosper Health — what's your name, and how can I help you today?"*. Triggers C. |
| C | All identity logic sequential — caller hears dead air. | Speculative race per `speculative_race.md`: on name detected, fire `find_patient_by_name_dob` + 4× `list_availability_slots` (today + next 3 biz days) in parallel. Pre-build create_patient payload as pure Python (no HTTP). Cancellation discipline. Fuzzy match @ 0.85-0.97 → confirm; ≥ 0.97 → proceed; multi-match → enumerate. |
| D | `_STATE_FILLERS` fires every IDENTIFY turn — artificial "this can take a few seconds" when lookup is ~30ms. | Predicted-latency gate at `bot.py:243`. Predict via `dispatcher.timing.summary()` p95 per tool in `ALLOWED_TOOLS[state]` + 300ms LLM baseline. Threshold 700ms. Default 400ms if no history. Drop "this can take a few seconds" timing-lie from `STATE_FILLERS["IDENTIFY_PATIENT"]`. |

---

## Implementation order

1. **D — Latency-gated fillers.** Smallest blast radius. Independent of A/B/C. Unblocks honest UX immediately. 1-2 hours including tests.
2. **B — New greeting opener.** ~5 minute change to `prompts.py::GREETING`. Independent. Required precursor to C (C consumes name from first turn).
3. **A — Interruption (TTSAudibleObserver).** Independent of C. Medium blast radius (`bot.py` + `dispatcher.py` + `prompts.py` persona delta + new processor). 3-5 hours including tests.
4. **C — Speculative race.** Largest. Builds on B. Touches dispatcher core (`_transition` → async, new module `speculation.py`). 1-2 days including tests + 5 new eval scenarios.

A and C are independent of each other (different files). Could parallel if comfort with merge.

---

## Subproblem A — Interruption (Design B)

### Files changed

- **NEW** `src/prosper/observers.py` — `TTSAudibleObserver(FrameProcessor)`. Accumulates `TTSTextFrame.text` between `BotStartedSpeakingFrame` and either `BotStoppedSpeakingFrame` (clear buffer) or `StartInterruptionFrame` (flush to dispatcher).
- **MODIFY** `src/prosper/bot.py`:
  - Insert `TTSAudibleObserver` in `Pipeline([...])` between `tts` and `transport.output()`.
  - Pass dispatcher reference (or callback) so observer can invoke `dispatcher.mark_last_assistant_interrupted(spoken_text)`.
  - Fix VAD docstring drift at lines 444-449 (says `min_volume=0.4, confidence=0.6`, code is `0.3 / 0.5`).
- **MODIFY** `src/prosper/dispatcher.py`:
  - New method `mark_last_assistant_interrupted(spoken_text: str) -> None`. Mutates `self.history[-1]['content']` to `f"{spoken_text}… [INTERRUPTED by user]"` if last role is assistant. Sequence guard: only mutates if not already interrupted.
- **MODIFY** `src/prosper/prompts.py`:
  - Append ~80-token paragraph to `CLINIC_PERSONA` explaining the `[INTERRUPTED by user]` marker. Stays well under cache budget.

### Tests

- **NEW** `tests/test_observers.py`:
  - `test_observer_records_text_between_speech_frames` — sequence of TTSTextFrames → BotStopped → assert buffer cleared (no interrupt).
  - `test_observer_flushes_on_start_interruption` — TTSTextFrames → StartInterruption → assert dispatcher.mark_last_assistant_interrupted called with concatenated text.
  - `test_observer_handles_empty_interruption` — interrupt before any TTSTextFrame → assert mark called with empty string.
- **MODIFY** `tests/test_dispatcher.py`:
  - `test_mark_last_assistant_interrupted_appends_marker` — pre-seeded history → call → assert last assistant content ends with `[INTERRUPTED by user]`.
  - `test_mark_last_assistant_interrupted_noop_if_not_assistant` — last message is user → call → no mutation.
- **MODIFY** `evals/mock_llm.py` + `evals/scenarios.py` (per A1 Q5):
  - Add `simulate_interrupt_after_turn(turn_index, partial_text)` helper for scenario authoring.
  - Add 2 scenarios: `barge_in_during_confirm` (cuts in mid-confirmation), `barge_in_correcting_slot` (cuts in mid slot-list).

### Open questions (A1 §6)

A1, A2, A3, A4 (see "Open questions" block at bottom).

---

## Subproblem B — Greeting line

### Files changed

- **MODIFY** `src/prosper/prompts.py`:
  - `GREETING` TASK_MESSAGES entry — replace with: *"Open with exactly this line: 'Hi, you've reached Prosper Health — what's your name, and how can I help you today?' Do not deviate. Once the caller answers, route via the appropriate intent."*
  - Keep ≤ 1 KB constraint per `test_each_task_message_under_1_kb`.
- **MODIFY** `src/prosper/dispatcher.py`:
  - In `_maybe_transition_from_user_text`: when state is GREETING and user_text is non-empty, transition to IDENTIFY_PATIENT *and* fire `await self._launch_speculation()` (added in C).
  - If C lands first, B is one prompt-string change.

### Tests

- **MODIFY** `tests/test_prompts.py` (or wherever GREETING test lives) — assert new opener is present, asserts the literal Prosper Health branding string.
- **MODIFY** existing greeting eval scenarios in `evals/scenarios.py` — judge prompts may currently match old line; update if any literal-match check exists.

---

## Subproblem C — Speculative race

Full design in `docs/research/speculative_race.md`. Summary:

### Files changed

- **NEW** `src/prosper/speculation.py` (full module per §9.1 of design):
  - `EXACT_THRESHOLD = 0.97`.
  - `SpeculativePatientPayload`, `SpeculationStore` dataclasses.
  - `_cancel_and_drain`, `cleanup_speculation`, `classify_find_result`, `build_disambiguation_message`, `next_n_business_days`.
- **MODIFY** `src/prosper/dispatcher.py`:
  - `SessionMemory.speculation: SpeculationStore | None = None`.
  - `_transition` → async OR add `_pre_transition_hook` async hook.
  - `_maybe_transition_from_tool` → async; insert `classify_find_result` + disambiguation injection.
  - `_maybe_transition_from_user_text` → async; call `await self._launch_speculation()` at GREETING→go_identify.
  - `handle_user_turn` already async — propagate `await` through chain.
  - New: `_launch_speculation`, `_on_avail_task_done`, `_consume_avail_prefetch`.
- **NO CHANGE** `src/prosper/flows.py`, `src/prosper/tools.py`, `src/prosper/ehr_client.py`.

### Tests

- **NEW** `tests/test_speculation.py` — 9 unit tests per design §10.1.
- **MODIFY** `tests/test_dispatcher.py` — 6 integration tests per §10.2.
- **MODIFY** `evals/scenarios.py` — 5 new scenarios per §10.3.

### Risks (A2 §12)

- `_transition` becoming async — wide mechanical change; needs full call-site audit.
- Two write paths to `memory.last_slots` — invariant: only write when empty.
- 4× HTTP GETs per call on EHR — fine at demo scale, future-gate by intent likelihood.

---

## Subproblem D — Latency-gated fillers

### Files changed

- **MODIFY** `src/prosper/bot.py`:
  - Lines 124-127: keep `_STATE_FILLERS` dict.
  - Lines 243-245: replace unconditional `TTSSpeakFrame(filler)` push with:
    ```python
    if _should_emit_filler(self._dispatcher, self._dispatcher.state):
        filler = _STATE_FILLERS.get(self._dispatcher.state)
        if filler is not None:
            await self.push_frame(TTSSpeakFrame(filler))
    ```
  - New helper `_should_emit_filler(dispatcher, state) -> bool` — sums `dispatcher.timing.summary()` p95 of each tool in `ALLOWED_TOOLS[state]` + 300ms LLM baseline; returns `predicted_ms >= FILLER_LATENCY_THRESHOLD_MS`.
- **NEW** `src/prosper/constants.py` (or top of `bot.py`):
  - `FILLER_LATENCY_THRESHOLD_MS = 700`.
  - `LLM_BASELINE_LATENCY_MS = 300`.
  - `DEFAULT_TOOL_LATENCY_MS = 400` (used when no history available).
- **MODIFY** `src/prosper/prompts.py`:
  - `STATE_FILLERS["IDENTIFY_PATIENT"]` — drop "this can take a few seconds" lie. Reduce to "One moment." or remove the IDENTIFY entry entirely.
  - Audit other STATE_FILLERS for accuracy.

### Tests

- **MODIFY** `tests/test_bot.py` (if exists) or **NEW** `tests/test_filler_gate.py`:
  - `test_filler_suppressed_when_predicted_latency_low` — mock dispatcher with summary returning p95=30ms for tools → assert no TTSSpeakFrame pushed.
  - `test_filler_emitted_when_predicted_latency_high` — mock summary returning p95=1500ms → assert TTSSpeakFrame pushed.
  - `test_filler_default_when_no_history` — empty summary → assert TTSSpeakFrame pushed (400ms × N tools likely > 700ms).
  - `test_filler_suppressed_in_states_with_no_tools` — GREETING, CHOOSE_INTENT, END → no filler.
- **MODIFY** `tests/test_prompts.py` — if there's a test asserting `STATE_FILLERS["IDENTIFY_PATIENT"]` starts with `"One moment."`, update to match new copy.

---

## Verification gates (every subproblem)

- `make verify` (ruff + format + mypy --strict + pytest) passes.
- `make mock-eval` passes (all 16 existing scenarios + new ones).
- For A and C: spot-check manually via `make ehr` + `make bot` and a live call.
- For D: log p95 of next-tool prediction in 5 mock-eval runs; confirm filler suppression triggers correctly.

---

## Open questions (block on before code)

1. **Interrupt marker exact text** — `[INTERRUPTED by user]` recommended. Alt: `[truncated]`, `<interrupted/>`, structured JSON. A1 recommendation: natural-language marker. **Confirm or override.**
2. **TURN_INTERRUPTED console-bus event** — emit one when interrupt fires (for operator UI), yes/no? Adds maintenance surface. A1 leans yes.
3. **`_INTERRUPTED_DENY` regex in dispatcher** — special-case "no no don't do that" intents when previous turn is `[INTERRUPTED]`? A1 recommends NO (let LLM decide). User said "no hard branching, context-aware" — confirms NO. **Confirm.**
4. **Self-interruptions** ("Tuesday at — no, Wednesday") — out of scope, handled by STT aggregator. **Confirm.**
5. **Eval-side `simulate_barge_in` helper** — add to `evals/mock_llm.py` so FSM scenarios can exercise the interruption path? **Yes/no.**
6. **VAD docstring drift fix at `bot.py:444-449`** — piggyback on Subproblem A commit? **Yes/no.**
7. **`EXACT_THRESHOLD = 0.97`** for fuzzy gate — accept or different?
8. **`FILLER_LATENCY_THRESHOLD_MS = 700`** — accept or different?
9. **A and C in parallel** (separate worktrees) or sequential? Both touch `_maybe_transition_from_user_text` lightly. Recommend sequential D → B → A → C.
10. **C scope reduction** — if 1-2 day estimate is too long, smallest valuable subset is: only `T_find` parallel (drop avail prefetch). Saves ~2/3 of the LoC. Recommend full scope; flag for reduction option.

---

## Effort estimate

- D: 1-2 hours
- B: 30 minutes (assuming no eval scenario rewrites needed)
- A: 3-5 hours (incl. tests + eval helper)
- C: 1-2 days (incl. full test suite + 5 new eval scenarios)

Total: **2-3 working days** sequential.
