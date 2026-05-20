# ADR-001 — Hybrid FSM with per-state tool whitelist

**Status:** Accepted (2026-05-19)
**Deciders:** Matías + LLM council (kimi-k2-0905 + gemini-2.5-pro + gpt-5-mini, chaired by Claude Sonnet)
**Council deliberation:** [`docs/superpowers/specs/2026-05-19-prosper-challenge-design.md`](../superpowers/specs/2026-05-19-prosper-challenge-design.md) §2.1

## Context

The agent has nine conversational states (greeting → identify → register? → choose → book/cancel → confirm → end). Each state can legally fire only a small subset of tools (e.g. `cancel_appointment` is illegal during `GREETING`). We needed an orchestration model that makes "agent did the wrong thing at the wrong time" structurally impossible — not just discouraged by prompting.

## Decision

A lightweight FSM (`src/prosper/flows.py` — plain data) drives high-level state transitions. Inside each state, an LLM gets full conversational freedom but is exposed **only** to that state's whitelisted tools. The dispatcher (`src/prosper/dispatcher.py`) enforces the whitelist at the call boundary: any tool call outside the active state's list is rejected with a structured `tool_rejected` event injected into the LLM history, and the model retries on the next iteration.

## Consequences

- **Positive:** State transitions become deterministic and trivially assertable in evals (`expected_terminal_state`). The "agent called `cancel_appointment` during greeting" class of bug is structurally eliminated. A single ~250 LOC file (`dispatcher.py`) is the complete machine — reviewers trace it in one read.
- **Negative:** Adding a new tool requires touching `flows.py` (whitelist), `tools.py` (handler + schema), and possibly transitions. Not a one-line change.
- **Operational:** Per-state task messages stay ≤ 1 KB (`test_each_task_message_under_1_kb`), keeping per-turn input cost bounded.

## Alternatives considered

1. **Single mega-prompt with all tools enabled.** Tried in the initial draft. First eval run produced a transcript where the model called `cancel_appointment` during `GREETING`. Rejected — too opaque to evals, hallucinates over long contexts.
2. **Pipecat Flows official package.** Mature and well-supported, but adds a separate dependency and `NodeConfig` boilerplate on the latency-sensitive path. Rejected for v1; migration later is a mechanical change since our `flows.py` topology is already declarative.
3. **Pure FSM with no LLM-in-state freedom.** Robotic; can't handle natural recovery ("wait, that's the wrong number"). Rejected unanimously by council.
