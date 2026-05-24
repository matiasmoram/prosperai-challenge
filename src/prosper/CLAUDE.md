# src/prosper/ — Front F2: Agent core (orchestration)

You are in the **agent-core** tree. This charter covers Front **F2**: the FSM
runtime, tool registry, LLM adapter, EHR client, reliability layers. Ownership
map + parallel-safety matrix: `../../FRONTS.md`. Stable architecture invariants:
root `../../CLAUDE.md`. How any feature is wired: `../../SOLUTION.md`.

> **Router — which front am I in?** This dir has two sub-fronts with their own
> CLAUDE.md. Read theirs instead if you're editing under them:
> - `ehr/`     → **Front F1** (data + API) — see `ehr/CLAUDE.md`
> - `console/` → **Front F5** (operator dashboard + bus) — see `console/CLAUDE.md`
> Everything else in this dir (the loose `.py` files below) is **F2**.

## What lives here (F2-owned)
- `dispatcher.py`  — FSM runtime + the **only** path to tools: per-state whitelist
  enforcement, handle redaction, memory validation, history pruning, console-bus wiring
- `flows.py`       — `State` enum, `ALLOWED_TOOLS[state]`, `TRANSITIONS` (plain data)
- `tools.py`       — 8 handlers + `TOOL_SCHEMAS` + `HANDLERS` map; all return `Result[Ok,Err]`
- `llm.py`         — `OpenAILLMAdapter`: tenacity retry, fallback model, usage surfacing
- `ehr_client.py`  — `EHRClient` (httpx) + X-Request-Id + SSRF-validated base URL
- `result.py`      — `Result[Ok,Err]` discriminated union
- `speculation.py` — fuzzy identity disambiguation (`classify_find_result`, `next_n_business_days`)
- `observers.py`, `observability/` — TTS-audible observer, `TimingCollector`, `redact_pii`
- `bot.py` (here + root `../../bot.py`) — Pipecat pipeline, `DispatcherProcessor`, env fail-fast, SSRF guard

## Contracts you MUST NOT break
- **The dispatcher is the only path to tools.** A direct `HANDLERS[name](...)`
  in product code defeats the per-state FSM whitelist. Never call a handler directly.
- **S1 — the spine (`tools.py` + `flows.py`) takes one editor at a time.** Adding a
  tool/state is a public-contract change (`Err.code`, `ALLOWED_TOOLS`) → it REQUIRES
  an `evals/scenarios.py` scenario + `make mock-eval` (root rule 4). That couples it to F7.
- **S3 — only F2 edits `bot.py`.** The `:7860` pipeline. F4 signals in from the browser; it never edits this.
- **The LLM never sees a UUID.** `_redact_for_llm` enumerates lists `[1] [2] …`;
  `_resolve_memory_handles` swaps the number back to a real id before any HTTP call.
  Write-tools validate against `SessionMemory` → hallucinated ids return
  `Err(hallucinated_slot_id|hallucinated_appointment_id)` without touching HTTP.
- **Reschedule is atomic, not cancel+rebook.** `reschedule_appointment` = one
  transaction with rollback-on-conflict. The legacy cancel-then-rebook chain fires
  ONLY on mid-cancel intent-flip. Reintroducing it for "reschedule" orphans state on conflict.
- **Latency is a feature.** Extra LLM round-trips, extra tool calls, synchronous
  waits in the pipeline = regressions. Fill silence with a `STATE_FILLERS` line, not a block.
- **Telemetry never breaks the call path.** Every `_publish` to the console bus is
  fire-and-forget, wrapped in try/except, no-op when no bus injected (tests/evals).

## Don't touch (other fronts)
- `prompts.py` — **Front F3.** All caller-audible copy. If your change only needs new
  wording, that's an F3 edit. You touch `prompts.py` only when adding a *state* (then it's S1-coupled).
- `ehr/**` — F1.  `console/**` — F5.  Static call UI — F4.

## Verify gate
```
make verify        # ruff + format + mypy --strict + pytest — before ANY commit
make mock-eval     # MANDATORY after any dispatcher/flows/tools change (~5s, no API key)
make tester        # offline tool-receipt gate — run after ANY change to outcome emission
```
A spine (S1) change that skips `make mock-eval` is incomplete. **`tester/` (a third
test surface, F7) guards the FSM's outcome accounting**: it treats an `outcome`
event (`booked`/`cancelled`/`rescheduled`) as a *claim* and a `tool_call_end ok`
as its *receipt* — change the dispatcher so a positive outcome can fire without its
write and `make tester` turns red. Adding a new outcome (e.g. F6 `handed_off`) means
teaching `tester/receipt_gate.py` whether it needs a receipt.

## Open work — derive fresh each cycle (manager's job), don't bake a TODO here
Live ledgers + a reconciliation warning:
- `../../FUTURE.md` §1.2 STT/TTS fallback, §3.1 streaming TTS, §3.2 slot prefetch,
  §3.3 speculative race (half-landed), §4.1 provider preference (hits S1), §4.2 notes (with F3).
- `../../SOLUTION.md` §14 in-flight + §17 priority order. **⚠ §14 is stale**: the
  mini-LLM specialty router appears shipped (ADR-005 + `tests/test_triage*.py`) — confirm
  in code, then move it out of §14 (root rule 11) before building "new" router work.
- `../../docs/testing/ADVERSARIAL_FINDINGS.md` — **F-001…F-013 are CLOSED** (root-fixed,
  79 tests assert the fixes). The "needs sign-off" lines are original proposals, not open TODOs.
- This branch (`feat/hybrid-llm-navigation`) has active uncommitted work — read the
  frontdesk-handoff spec/plan in `docs/superpowers/` before editing dispatcher routing.
