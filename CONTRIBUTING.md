# Contributing

Thanks for taking the time to read the code. This guide gets you from a fresh
clone to a green PR in ~5 minutes.

## Setup

```bash
make install              # uv sync — installs deps into .venv
cp env.example .env       # fill in OPENAI_API_KEY + ELEVENLABS_API_KEY
make seed                 # populates data/ehr.db with 3 patients + slots
```

Two terminals for live dev:

```bash
make ehr                  # FastAPI EHR on :8000
make bot                  # pipecat voice loop (needs a SmallWebRTC transport)
```

## Dev loop

Edit → `make test` → `make lint` → `make type` → commit. The `make verify`
target runs everything CI runs (ruff + format + mypy + pytest) in one shot —
use it before pushing.

`pre-commit` runs ruff/format/mypy/pytest automatically on every commit (see
`.pre-commit-config.yaml`); a failure aborts the commit. Don't bypass with
`--no-verify` — fix the failure or escalate in the PR description.

## Where things live

- `docs/architecture.md` — high-level diagram of bot ↔ dispatcher ↔ FSM ↔ EHR
- `docs/adr/` — the three load-bearing decisions (hybrid FSM, separate EHR
  process, paired state+judge eval)
- `docs/glossary.md` — repo-specific vocabulary (TTFT, dispatcher, etc.)
- `CLAUDE.md` — hard rules for both human and LLM contributors. Read first.
- `SOLUTION.md` — what was built and why, for reviewers
- `docs/research/` — point-in-time research notes that informed each wave

## Adding a new tool

1. Define the handler in `src/prosper/tools.py` returning `Result[Ok, Err]`
   and add it to the `HANDLERS` and `TOOL_SCHEMAS` dicts.
2. Whitelist it in `src/prosper/flows.py` → `ALLOWED_TOOLS[State.X]` for
   whichever states are allowed to call it. Other states will refuse.
3. Add a `Scenario(...)` in `evals/scenarios.py` that exercises both the
   happy path and at least one error code (`Err.code` is public contract).

## Adding a new state

1. `src/prosper/flows.py` — add the variant to the `State` enum, an
   `ALLOWED_TOOLS` row, and the edges in `TRANSITIONS`.
2. `src/prosper/prompts.py` — add a `TASK_MESSAGES[State.X]` ≤ 1 KB
   (enforced by `test_each_task_message_under_1_kb`).
3. `src/prosper/dispatcher.py` — wire the transition trigger (tool result
   code or short-circuit keyword).
4. `tests/test_flows.py` + a scenario in `evals/scenarios.py`.

## Adding an eval scenario

Append a `Scenario(...)` dataclass to `evals/scenarios.py`, then:

```bash
python -m evals --only my_scenario      # quick single-scenario run
python -m evals --json evals/results/current.json --baseline evals/results/baseline.json
```

## PR checklist

- [ ] `make verify` green (lint + format + mypy + tests)
- [ ] Behaviour change? eval baseline diff is intentional and explained
- [ ] New tool / state / scenario follows the recipes above
- [ ] `CLAUDE.md` rules respected (Result type, prompt size, voice copy
      location, FSM as source of truth)
