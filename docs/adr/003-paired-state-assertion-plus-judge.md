# ADR-003 — Paired state-assertion + LLM judge for every scenario

**Status:** Accepted (2026-05-19, hardened 2026-05-20 after a real eval miss)
**Deciders:** Matías + LLM council
**Council deliberation:** [`docs/superpowers/specs/2026-05-19-prosper-challenge-design.md`](../superpowers/specs/2026-05-19-prosper-challenge-design.md) §2.8

## Context

LLM-as-judge is the canonical 2026 eval pattern for voice agents: pass the transcript to a cheap LLM, get PASS/FAIL with justification against natural-language criteria. But judge-only evals have a known failure mode — the judge can mark a scenario PASS on a transcript where the assistant said "I've cancelled it for you" but no `cancel_appointment` tool call ever fired. The bot lied; the judge believed it. We hit this gap on a real adversarial scenario before adding the second check.

## Decision

Every scenario has **two checks, both must pass**:

1. **State assertion (deterministic):** snapshot DB row counts before/after, assert delta against `StateExpectation` (patient_count_delta, active_appointment_count_delta, cancelled_appointment_count_delta). Verify the FSM reached `expected_terminal_state`. Verify `expected_tool_call_codes` all fired and `forbidden_tool_calls` never did. Run a regex post-check (`_HALLUCINATED_CLAIM`) against assistant turns — if the bot claims "I cancelled it" with no matching `tool_ok` within a 4-event window, fail with reason.
2. **LLM judge (semantic):** gpt-4o-mini grades the transcript against `judge_criteria` (e.g. "did the bot read back the appointment time before cancelling?"). Catches things state can't measure — tone, confirmation, refusal quality.

## Consequences

- **Positive:** The hallucinated-confirmation class of bug becomes structurally catchable. Adversarial scenarios (`hallucinated_confirmation_trap`, `cross_patient_cancel_refusal`) are actually meaningful, not just judge-vibes. Reviewers see two PASS marks per scenario in the CLI output (`state=P judge=P`).
- **Negative:** Each scenario needs both `StateExpectation` and `judge_criteria` written. Slight cost in scenario authoring (mitigated by the dataclass — adding a scenario is a 20-line PR).
- **Operational:** `make eval` exits 0 on all-pass, 1 on any-fail, 3 on `--baseline` regression (a scenario that was passing in the snapshot now fails).

## Alternatives considered

1. **Judge-only.** Cheaper, faster to author. Rejected after hitting the lie-bot bug in v1 — the judge marked a transcript PASS when the bot fabricated a cancellation.
2. **State-only.** Catches mutations but misses semantic quality ("did the bot confirm before writing?"). Rejected — too narrow for a conversational agent.
3. **Judge + state, OR-gated (either passes → scenario passes).** Defeats the purpose. Rejected.
