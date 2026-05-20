# Post-submission improvements — execution plan

**Source:** 4 parallel subagent reports in `docs/research/2026-05-20-*.md`.
**Order:** correctness first → reliability (README ask) → eval depth (README ask) → latency polish → docs.

## Phase A — Audit bugs (correctness blockers)

- [ ] A1 `repository.py:list_available_slots` — also filter `start_at >= datetime.now(UTC)` so past slots aren't offered.
- [ ] A2 `repository.py:create_appointment` — wrap `session.commit()` in try/except for `IntegrityError`; re-raise as `SlotTakenError(slot_id, owner_patient_id=?)` so the partial-unique-index race surfaces as 409 not 500.
- [ ] A3 `dispatcher.py:_record_tool_result` — strip raw UUIDs from the LLM-visible tool result string. Keep them in `transcript`/`memory` for our own logic but only feed the LLM a redacted human-readable summary. Stops the model from reading IDs aloud.
- [ ] A4 `dispatcher.py:_execute_tool` — for `create_appointment` and `cancel_appointment`, validate that `slot_id`/`appointment_id` came from a recent `last_slots`/`last_upcoming_appointments` window. If hallucinated, inject a synthetic `Err(code="hallucinated_id")` without calling the EHR.

## Phase B — Reliability (README bonus #2)

- [ ] B1 `src/prosper/llm.py` — wrap `client.chat.completions.create` with `tenacity` retry (3 attempts, exponential jitter, retry on 429/5xx/transient). One test: mock raises 500 twice then succeeds.
- [ ] B2 startup health-check — `src/prosper/bot.py` runs a tiny EHR `/health` GET + an OpenAI 1-token ping; logs warning if either fails. Doesn't block start (single clinic, single dev box) but surfaces the failure pre-call.
- [ ] B3 Add an `OPENAI_FALLBACK_MODEL` env var (e.g. `gpt-4o`) — when the primary model raises after retries, swap once to the fallback model. Single line in `OpenAILLMAdapter`. Skip multi-provider gateway scope.

## Phase C — Eval depth (README bonus #3)

- [ ] C1 `evals/scenarios.py` — add 5 adversarial scenarios from `2026-05-20-eval-depth-research.md`:
  - `prompt_injection_direct_override`
  - `prompt_injection_stored_in_name`
  - `cross_patient_cancel_refusal`
  - `hallucinated_confirmation_trap`
  - `off_topic_steering_and_budget`
- [ ] C2 `evals/runner.py` — enforce `forbidden_tool_calls` mid-sequence (fail fast, don't wait for end-of-run); add a `hallucinated_confirmation` post-check via regex over the assistant transcript for "I (have )?cancelled" / "booked" claims with no matching tool-ok event.
- [ ] C3 add `adversarial` tag column to `_summary` so `make eval` reports adversarial-pass-rate separately.

## Phase D — Latency polish (low risk wins)

- [ ] D1 `bot.py:DispatcherProcessor` — before each `handle_user_turn` call that lives in a slow state (IDENTIFY_PATIENT/BOOK_FLOW/CANCEL_FLOW = tool-firing states), push a single filler `TTSSpeakFrame("one moment…")`. Adds perceived responsiveness for tool-call states.
- [ ] D2 `bot.py:run_bot` — ElevenLabs Flash v2.5 swap (constructor only, zero test risk per research): `model="eleven_flash_v2_5"` (or current SDK constant) on `ElevenLabsTTSService`.

## Phase E — Docs

- [ ] E1 `SOLUTION.md` — add endpoint-mapping table (challenge-name → REST route) so a pedantic reviewer's "where is `find_patient`?" is answered in the doc.
- [ ] E2 `SOLUTION.md` — update "Future work" + intentional-cuts to reflect what's now in v2 (LLM retry/fallback, adversarial scenarios, filler speech, Flash v2.5, audit fixes).
- [ ] E3 Replace transcript placeholders with the canned-transcript pattern: an actual sample dispatched through the eval runner with judge ON.

## Phase F — Gate

- [ ] F1 `uv run pytest tests/ evals/ -q` — must pass (currently 42 unit + 2 eval-types = 44).
- [ ] F2 `uv run ruff check src/ tests/ evals/` + `uv run ruff format --check src/ tests/ evals/` — must be clean.
- [ ] F3 git commit per phase.
