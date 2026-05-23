# CLAUDE.md

Orientation for any Claude session in this repo. Read before changing anything substantial.

## Project in one line

Two-process voice agent for the Prosper Health challenge: Pipecat bot (`:7860`) ↔ custom FSM dispatcher ↔ FastAPI EHR (`:8000`) on SQLite. Full narrative in `SOLUTION.md`; topology + FSM graph in `docs/architecture.md`; load-bearing choices in `docs/adr/001..004`.

## Commands (all in `Makefile`)

- `make verify` — pre-submit gate: ruff check + format --check + `mypy --strict` + pytest. Run before every commit; CI runs the same.
- `make test` — `pytest tests/ -v`.
- `make mock-eval` — all 16 scenarios offline via `evals/mock_llm.py` in ~5 s, zero API cost. Run after any dispatcher/flows/tools change.
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
- **Identity gate is a hard checkpoint.** Booking / cancel / reschedule tools are mounted only after `SessionMemory.identified_patient` is set. Identify state exposes only `find_patient_by_phone` / `find_patient_by_name_dob` / `create_patient`.
- **`Result[Ok, Err]` everywhere.** Tool handlers in `src/prosper/tools.py` return `Result` (`src/prosper/result.py`). `Err.code` is the **public eval contract** — `evals/scenarios.py` asserts on these codes; renaming one is a breaking change, update scenarios in the same commit.
- **LLM never sees a UUID.** `_redact_for_llm` scrubs ids; lists are enumerated `[1]`, `[2]` … and the dispatcher's `_resolve_memory_handles` swaps those numbers back to real UUIDs before any HTTP call. `create_appointment` / `cancel_appointment` validate against `SessionMemory` (`identified_patient`, `last_slots`, `last_upcoming_appointments`); hallucinated ids return `Err(code="hallucinated_slot_id" | "hallucinated_appointment_id")` without touching HTTP.
- **DB invariants live in the schema.** One active appointment per slot is a partial unique index in `src/prosper/ehr/models.py`; race conditions surface as `409 slot_taken`, never 500. Past slots are filtered in `list_availability_slots`.
- **Slot datetimes are naive UTC.** Seed converts NY-local → UTC → strips tzinfo. Any new code reading/writing `slot.start_at` must follow the same convention; mixing naive-local with naive-UTC silently misreads availability.
- **All caller-audible copy lives in `src/prosper/prompts.py`.** `CLINIC_PERSONA` (~1100 tokens) is intentionally long for OpenAI prompt-cache hits — don't trim. Per-state `TASK_MESSAGES`, `STATE_FILLERS`, and `FALLBACK_LINES` must stay there (≤ 1 KB per `TASK_MESSAGES` entry — enforced by `test_each_task_message_under_1_kb`). Inline voice strings in `bot.py` / `dispatcher.py` are a regression.
- **Eval = paired state-assertion + LLM judge.** A scenario passes only if both `ScenarioResult.state_pass` and `ScenarioResult.judge_pass` are true (ADR 003). Judge-only passes are a known false-positive pattern.
- **Bot entrypoint guard.** `PROSPER_BOT_ENTRYPOINT=1` makes missing env vars `SystemExit(2)` *before* the ~17 s pipecat import wall. Tests leave it unset on purpose — keep it that way.

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

- `docs/architecture.md` — process topology + FSM graph.
- `docs/adr/001..004` — hybrid FSM + whitelist, separate EHR process, paired state+judge eval, operator console event stream.
- `CONTRIBUTING.md` — recipes for adding a tool / state / scenario.
- `SOLUTION.md` — design + decision trail (reviewer-facing).
- `docs/glossary.md` — TTFT, dispatcher, FSM, judge.
- `SECURITY.md` — SSRF guard on `PROSPER_EHR_URL`, PII redaction, threat model.
- `ERRORS.md` — honest snapshot of known live-eval failures.
- `FUTURE.md` — ranked next features.
