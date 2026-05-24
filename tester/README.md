# `tester/` — catching agent mistakes without hand-dialing

Prototype layer that grew out of the challenge's evaluation ask:

> *ways to automatically test or simulate calls so that hallucinations and agent
> mistakes can be caught without having to dial in by hand every time.*

The full brainstorm (tiered eval pyramid, the synthetic-caller / hallucination /
methodology survey, and the build order) lives in **`SOLUTION.md` §11**. It
complements `evals/` — it does not replace it.

Two pieces, both auditing the same invariant from different ends:

1. **Tool-receipt gate** — offline, $0, deterministic regression guard.
2. **Autonomous call simulator** — live, the LLM plays goal-seeking adversarial
   callers against the real bot, no scripted turns, no human dialing.

## Part 1: the tool-receipt hallucination gate (offline)

The agent's worst failure mode is **confirmed-but-didn't-happen** — telling the
caller "you're booked for Tuesday 3pm" when `create_appointment` returned an
error, or the id was invented.

The gate is the NABAOS *tool-receipt* pattern at its smallest. Over the operator
console event stream it treats:

- the terminal **`outcome`** event (`booked` / `cancelled` / `rescheduled`) as a
  **claim**, and
- each **`tool_call_end`** with `outcome == "ok"` as a **receipt**.

A positive claim with no matching receipt earlier in the same session is a
`Violation`. `refused` / `abandoned` claim nothing, so need no receipt.

| File | Role |
|---|---|
| `receipt_gate.py` | Pure logic: `check_receipts(events) -> list[Violation]`. No I/O. |
| `recorder.py` | `RecordingBus` + `record_call(scenario)` — drives a scenario through the **offline mock LLMs** with a capturing bus attached, returns the event stream. |
| `test_receipt_gate.py` | Unit tests (hand-built tampered streams) + integration (every mock scenario must back its outcome). |

### Why over the bus, not the transcript

The transcript regex in `evals.runner._check_hallucinated_confirmation` already
guards the **spoken words** ("I've cancelled that" with no `tool_ok`). This gate
guards the **FSM's own outcome accounting** — a different surface that a refactor
could silently break. The two are complementary, by design (council scope).

It is a **regression guard**: green today because the dispatcher only emits
`booked` after a successful `create_appointment` (and so on). It turns red the
moment someone changes the FSM so a positive outcome can fire without its write.

```bash
make tester        # uv run pytest tester/ -v  — offline, $0, no API key
```

## Part 2: the autonomous call simulator (live)

This is the literal *"simulate calls without dialing by hand"* deliverable. The
LLM plays a goal-seeking **caller** (`tester/personas.py`) — a persona + goal +
stop condition, generating each turn itself — against the **real bot** (the
dispatcher driven by OpenAI). A `RecordingBus` captures the call; afterwards
`tester/invariants.py` audits it.

| File | Role |
|---|---|
| `personas.py` | `Persona` + a curated adversarial library (confused elderly, wrong-then-corrected DOB, mid-call cancel→reschedule flip, prompt-injection name, demands a slot never offered, rude-but-completes, off-topic, hallucination bait). `generate_personas()` asks the LLM to invent fresh ones — fully automatic. |
| `live_sim.py` | `simulate_call(persona)` — seeds an isolated EHR, runs the real bot + LLM caller to END, returns the events + transcript. |
| `invariants.py` | `check_call()` — invariants that hold for **any** call: no unbacked outcome (the receipt gate) + no spoken hallucinated confirmation. |
| `simulate.py` | CLI. `python -m tester.simulate`. |
| `test_live_sim.py` | One live smoke test, gated on `PROSPER_EVAL_LIVE=1` so `make verify` never spends tokens. |

Key point: an adversarial caller *failing to get what it wanted is not a
failure* — a bot that refuses an injection or an invented slot is correct. The
simulator only flags a **dishonest confirmation**. Exit non-zero = a real
hallucination was caught.

```bash
make simulate                          # all curated personas (needs OPENAI_API_KEY in .env)
make simulate ARGS="--generate 5 -v"   # LLM invents fresh personas, print transcripts
make simulate ARGS="--only hallucination_bait"
```

## Roadmap (council build order)

1. ✅ **Tool-receipt gate** — smallest code, kills the worst failure mode.
2. ✅ **Autonomous adversarial call simulator** — live persona callers, no hand-dialing.
3. ⏳ **Hypothesis `RuleBasedStateMachine` over the dispatcher** — generate
   random-but-valid caller-action *programs* (`@rule` = give phone / give DOB /
   pick slot / change mind) and assert FSM invariants every dialog (tool ∈
   `ALLOWED_TOOLS[state]`, no UUID to the LLM, handle round-trip, no double-book,
   confirm-only-after-successful-write). Finds whole bug *classes* via shrinking,
   offline and $0 — the cheap complement to the live simulator.
