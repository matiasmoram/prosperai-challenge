# CLAUDE.md

Orientation for any Claude session in this repo. Read before changing anything substantial.

## Project in one line

Two-process voice agent for the Prosper Health challenge: Pipecat bot (`:7860`) ↔ custom FSM dispatcher ↔ FastAPI EHR (`:8000`) on SQLite.

## Read this first if you're new (or stale)

**`SOLUTION.md` is the canonical "how does X work" reference.** It walks the whole codebase section by section (process topology, FSM, tools, dispatcher mechanics, operator console, EHR, prompts, eval suite, in-flight work, file map). Anytime you're unsure how a feature is wired — or need to know what's shipped vs still-being-built — read the relevant SOLUTION.md section before grepping. The §14 *In-flight work* section is the source of truth for unfinished features (e.g. mini-LLM specialty router) so you don't duplicate work another agent is already doing.

Other entry points: `docs/architecture.md` (process + FSM diagrams), `docs/adr/001..004` (load-bearing decisions).

## Commands (all in `Makefile`)

- `make verify` — pre-submit gate: ruff check + format --check + `mypy --strict` + pytest. Run before every commit; CI runs the same.
- `make test` — `pytest tests/ -v`.
- `make mock-eval` — all scripted scenarios offline via `evals/mock_llm.py` in ~5 s, zero API cost. Run after any dispatcher/flows/tools change. (Suite has grown beyond the original 16 — see `SOLUTION.md` §11.)
- `make eval` / `make eval-baseline` — live OpenAI evals (needs `PROSPER_EVAL_LIVE=1` + `OPENAI_API_KEY`). `eval-baseline` exit `3` = a previously-passing scenario regressed.
- `make ehr` / `make bot` — the two live processes (separate terminals; `make seed` once first).
- `make status` — repo health snapshot.

Single test / scenario:

```bash
uv run pytest tests/test_dispatcher.py::test_name -v
uv run pytest tests/ -k phone -v
uv run python -m evals --only book_happy_path --mock-llm
```

`pytest.ini_options` sets `asyncio_mode = "auto"` — async tests need no decorator.

## What the bot does

Voice agent (Pipecat + ElevenLabs STT/TTS + OpenAI LLM) for a US clinic, business hours Mon-Fri 9-17 America/New_York, English, multi-specialty, 30-min slots. Identifies caller → registers if new → books / cancels / reschedules.

## Architecture you need before editing

- **Two processes, one DB.** `bot.py` (Pipecat + Dispatcher) → `src/prosper/ehr/api.py` (FastAPI) over HTTP via `EHRClient` (`httpx.AsyncClient`). Evals mount the EHR in-process via `httpx.ASGITransport` — no socket, fully hermetic.
- **Custom FSM, not `pipecat-flows`.** `src/prosper/flows.py` owns `State`, `ALLOWED_TOOLS[state]`, `TRANSITIONS`. `src/prosper/dispatcher.py` is the single gatekeeper: every LLM `tool_call` is checked against the per-state whitelist; forbidden calls become a `tool_rejected` event with a system note fed back to the model. **Never call a handler directly** — always through the dispatcher.
- **Identity gate is a hard checkpoint.** Booking / cancel / reschedule tools are mounted only after `SessionMemory.identified_patient` is set. IDENTIFY_PATIENT exposes only `find_patient_by_phone` / `find_patient_by_name_dob`; `create_patient` lives in REGISTER_PATIENT (see `flows.py` `ALLOWED_TOOLS` and `SOLUTION.md` §5).
- **`Result[Ok, Err]` everywhere.** Tool handlers in `src/prosper/tools.py` return `Result` (`src/prosper/result.py`). `Err.code` is the **public eval contract** — `evals/scenarios.py` asserts on these codes; renaming one is a breaking change, update scenarios in the same commit.
- **LLM never sees a UUID.** `_redact_for_llm` scrubs ids; lists are enumerated `[1]`, `[2]` … and the dispatcher's `_resolve_memory_handles` swaps those numbers back to real UUIDs before any HTTP call. `create_appointment` / `cancel_appointment` validate against `SessionMemory` (`identified_patient`, `last_slots`, `last_upcoming_appointments`); hallucinated ids return `Err(code="hallucinated_slot_id" | "hallucinated_appointment_id")` without touching HTTP.
- **DB invariants live in the schema.** One active appointment per slot is a partial unique index in `src/prosper/ehr/models.py`; race conditions surface as `409 slot_taken`, never 500. Past slots are filtered in `list_availability_slots`.
- **Slot datetimes are naive UTC.** Seed converts NY-local → UTC → strips tzinfo. Any new code reading/writing `slot.start_at` must follow the same convention; mixing naive-local with naive-UTC silently misreads availability.
- **All caller-audible copy lives in `src/prosper/prompts.py`.** `CLINIC_PERSONA` (~1100 tokens) is intentionally long for OpenAI prompt-cache hits — don't trim. Per-state `TASK_MESSAGES`, `STATE_FILLERS`, and `FALLBACK_LINES` must stay there (≤ 1 KB per `TASK_MESSAGES` entry — enforced by `test_each_task_message_under_1_kb`). Inline voice strings in `bot.py` / `dispatcher.py` are a regression.
- **Eval = paired state-assertion + LLM judge.** A scenario passes only if both `ScenarioResult.state_pass` and `ScenarioResult.judge_pass` are true (ADR 003). Judge-only passes are a known false-positive pattern.
- **Bot entrypoint guard.** `PROSPER_BOT_ENTRYPOINT=1` makes missing env vars `SystemExit(2)` *before* the ~17 s pipecat import wall. Tests leave it unset on purpose — keep it that way.
- **Reschedule is atomic, not cancel+rebook.** `reschedule_appointment` is a single transaction with rollback-on-conflict — if the new slot is held, the original appointment is preserved. The legacy cancel-then-rebook chain only fires when the caller flips intent mid-cancel (`dispatcher._maybe_transition_from_tool`). Don't reintroduce cancel-then-rebook for the "reschedule" intent; it produces orphan state on slot conflict. See `SOLUTION.md` §6.
- **Operator console bus is fire-and-forget.** `src/prosper/console/` ships 8 typed events (`state_change`, `transcript_turn`, `tool_call_start`/`end`, `latency_tick`, `patient_identified`, `slots_offered`, `outcome`). Adding a tool or state means deciding which events fire and threading `_redact_tool_args` so PII never reaches the bus. Telemetry MUST NEVER break the call path — every publish site is wrapped in `try/except` and the bus has bounded queues with overflow drop. See `SOLUTION.md` §8.
- **`specialty` is a string filter on `list_availability_slots`.** Today the LLM picks the value from caller intent ("Therapist", "Psychiatrist", "General Practice", "Dermatologist", "Physiotherapist"). A mini-LLM complaint→specialty router is **in flight** — read `SOLUTION.md` §14 before editing anything in `tools.py`, `flows.py`, or specialty-related scenarios so two agents don't conflict.

## Hard rules (apply to every change)

1. **Fix root causes.** No `try/except: pass`, no flags whose only purpose is skipping a failing test, no hardcoded values to make a test green.
2. **Tools return `Result[Ok, Err]`** — never bare `dict | None`. `Err.code` is public contract.
3. **The dispatcher is the only path to tools.** Direct `HANDLERS[name](...)` in product code defeats the FSM whitelist.
4. **Adding a tool or state requires an eval scenario** in `evals/scenarios.py` (recipe in `CONTRIBUTING.md`). Run `make mock-eval`.
5. **EHR is the source of truth, not bot context.** After any write, the next caller-confirmation message must come from a read (e.g. `get_upcoming_appointments`). A tool returning `Err` → apologize, never confirm. Hallucinated success is the worst failure mode here.
6. **Slot UX is adaptive, not a menu.** Ask rough preference (day/time-of-day) first. If specialty is booked → propose 2-3 free slots. If wide open → invert: "tell me when, I'll check". Always confirm before commit. Never dump full lists.
7. **Latency is a feature.** Extra LLM round-trips, extra tool calls, or synchronous waits in the pipeline are regressions. Fill silence with a one-line `STATE_FILLERS` entry rather than block.
8. **`prompts.py`** — ≤ 1 KB per `TASK_MESSAGES` entry; persona preamble stays long for cache benefit. All caller-audible strings stay in this file.
9. **Every public function in `src/prosper/`** has a type annotation and one-line docstring. `mypy --strict` is enforced via `make verify` and `pre-commit`.
10. **Don't bypass `pre-commit`.** Ruff + format + `mypy --strict` + tests run on every commit; `--no-verify` is not an out.

## Gotchas

- **Windows + emoji.** Startup prints with emoji `UnicodeEncodeError` on default `cp1252` console. Run with `$env:PYTHONIOENCODING='utf-8'` in PowerShell.
- **EHR auto-seed only on empty DB.** Re-seed after schema changes: delete `data/ehr.db` then `make seed`.
- **409 from EHR is terminal**, not retryable: slot taken or appointment not in scheduled state. Don't loop tenacity over it.
- **`other solutions/`** is a read-only reference dump from other candidates. Do not edit, do not import from.

## Where to look first

- **`SOLUTION.md` — start here.** Section-by-section walkthrough of every part of the codebase as it stands today, plus §14 *In-flight work* for unfinished features.
- `docs/architecture.md` — process topology + FSM graph.
- `docs/adr/001..004` — hybrid FSM + whitelist, separate EHR process, paired state+judge eval, operator console event stream.
- `CONTRIBUTING.md` — recipes for adding a tool / state / scenario.
- `docs/glossary.md` — TTFT, dispatcher, FSM, judge.
- `SECURITY.md` — SSRF guard on `PROSPER_EHR_URL`, PII redaction, threat model.
- `ERRORS.md` — honest snapshot of known live-eval failures.
- `FUTURE.md` — ranked next features.
- `docs/plans/`, `docs/research/` — active planning + research notes (may include WIP from other agents — read but don't edit).
