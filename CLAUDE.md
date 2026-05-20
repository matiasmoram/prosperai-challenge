# Repository working agreement (LLM-facing)

These rules apply to any LLM or human writing code in this repo. They are
**hard rules** — violating one means the change is incorrect, regardless of
whether it compiles or passes tests.

## Design

1. **Fix root causes.** Never silence errors with `try/except: pass`. Never
   add a flag whose only purpose is to skip a failing test. If a failing
   test points to a real bug, fix the bug.
2. **Tool handlers return `Result[Ok, Err]`** (see `src/prosper/result.py`).
   Never return bare `dict | None` from a tool. The `Err.code` is part of
   the public eval contract — do not rename a code without updating the
   scenarios that assert on it.
3. **Per-state system prompts in `prompts.py` stay ≤ 1 KB.** Test
   `test_each_task_message_under_1_kb` enforces this. The persona preamble
   stays ≥ ~1100 tokens for prompt-cache benefit; do not edit casually.
4. **Voice copy lives in `prompts.py`,** never inline in `dispatcher.py`.

## State machine

5. **The dispatcher is the single source of truth for which tools fire.**
   Bypassing it with a direct `HANDLERS[name](...)` call defeats the FSM
   safety net. If you need a new tool, add it to `ALLOWED_TOOLS` for the
   correct state in `flows.py` and add a scenario that exercises it.
6. **Every state transition is logged** as `{kind: "transition"}` in the
   transcript. Tests assert on terminal state via this log.

## Eval suite

7. **Adding a feature requires adding (or extending) at least one eval
   scenario.** Scenarios live in `evals/scenarios.py` as `Scenario`
   dataclasses; runtime is plain data, no framework changes.
8. **State assertions and the LLM judge must both pass** for a scenario
   to pass (`ScenarioResult.overall_pass`). Judge-only passes are a known
   anti-pattern (the judge can hallucinate success on transcripts that
   didn't actually mutate the EHR).

## Quality

9. **Every public function in `src/prosper/`** has a type annotation and a
   one-line docstring.
10. `pre-commit` (ruff format + ruff check + unit tests) runs on every
    commit. CI enforces the same on every push. `mypy --strict` is the
    next quality-gate to add — kept out of the v1 pre-commit because the
    repo's current type-coverage isn't strict-clean and we did not want
    to ship a broken hook.
