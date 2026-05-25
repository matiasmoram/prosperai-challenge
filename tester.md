# tester.md — how testing works (AI and non-AI)

This repo tests the voice agent at **four layers**, two of them AI-driven and two
deterministic. This doc explains each: what it proves, how to run it, and how big
it is. For *which cases* are covered, see `cases.md`; for the prototype design
notes, see `tester/README.md`.

---

## The big picture

| Layer | AI? | Cost | What it proves | Where |
|-------|-----|------|----------------|-------|
| **1. Unit + integration tests** | No | $0 | every function, FSM edge, EHR invariant, redaction | `tests/` (pytest) |
| **2. Mock-eval scenarios** | No (scripted LLM) | $0 | end-to-end call flows against the **real** dispatcher + **real** EHR | `evals/` |
| **3. Tool-receipt + clarification gates** | No | $0 | no hallucinated outcome; bot re-prompts on garble | `tester/` |
| **4. Live persona / messy-human sim** | **Yes** (OpenAI) | tokens | real LLM bot vs LLM caller, incl. disfluent/garbled speech | `tester/` |

Layers 1–3 run in `make verify` and cost nothing. Layer 4 needs `OPENAI_API_KEY`
and is run on demand. **A scenario or sim run is honest because the assertions
measure real effects** (DB row deltas, FSM terminal state, tool receipts) — never
the mock's own scripted output echoed back.

---

## Layer 1 — Non-AI unit + integration tests (pytest)

The bedrock. **712 tests collected** (1 skipped — the live smoke test), all
offline, deterministic, run in ~30 s.

Breakdown (test functions by area):
- `tests/` core (dispatcher, flows, tools, llm, prompts, result, observers, timing,
  triage, route_intent, barge-in, llm-failure, …) — the FSM + adapters.
- `tests/adversarial/` (~74) — one file per closed finding F-001…F-013 + standing
  fuzz / hostile-LLM / invariant guards.
- `tests/ehr/` (~85) — FastAPI + repository + schema: availability, 409 races,
  atomic reschedule, max-length caps, calendar range.
- `tests/console/` (~51) — 9 typed events, bus isolation, audit redaction, SSE replay.
- `tests/integrations/` (~16) — MailStore (round-trip, PII, traversal guard), `/frontdesk`.
- `tester/` (~51) — the harness's own unit tests (receipt-gate, personas, noise, clarification).

Run: `uv run pytest tests/ -q` · single: `uv run pytest tests/test_dispatcher.py::test_name -v`.
`asyncio_mode = "auto"` — async tests need no decorator.

## Layer 2 — Non-AI mock-eval scenarios (`evals/`)

**107 scenarios**, each a scripted caller conversation driven through the **real
dispatcher** and a **real in-process EHR** (mounted via `httpx.ASGITransport` — no
socket). Only the *LLM* is mocked: `MockDispatcherLLM` scripts the model's tool-call
decisions turn by turn, and `MockTriageClient` keyword-routes symptoms. Everything
downstream — FSM whitelist, memory validation, EHR writes — is the production code.

Why it's not tautological: each scenario asserts a **`StateExpectation`** —
`patient_count_delta` / `active_appointment_count_delta` / `cancelled_…_delta`
computed by `SELECT count(*)` on the real SQLite DB **before vs after** the call,
plus the FSM terminal state, the tool-call sequence, and forbidden tool calls. The
mock controls what the bot *says*; the DB measures what actually *happened*.

**Paired contract (ADR 003):** a scenario passes only if the state assertion **and**
the LLM judge both pass. (In mock mode the judge is stubbed; live eval uses a real
judge.) Tags: `happy ×27`, `edge ×60`, `recovery ×19`, `adversarial ×19`, plus
reschedule / identity / triage / duration / handoff / hybrid / hallucination / …

Run: `uv run python -m evals --mock-llm` (~5 s, $0) · one: `--only book_happy_path --mock-llm`.

## Layer 3 — Non-AI standing gates (`tester/`)

Deterministic invariants over the recorded event stream — they need no LLM because
they audit *structure*, not language.

- **Tool-receipt gate** (`tester/receipt_gate.py`): the terminal `outcome` event
  (booked/cancelled/rescheduled) is a *claim*; each `tool_call_end outcome=="ok"` is
  a *receipt*. A positive outcome with no matching receipt = violation. Guards against
  the FSM reporting success without a real write. `handed_off` is a no-claim outcome.
- **Clarification contract** (`tester/clarification.py`): `check_recovers_gracefully`
  flags `plowed_ahead_on_garble` when a write tool consumes a garbled caller turn with
  no intervening clarification/confirm. `detect_clarification` is anchored to the real
  `FALLBACK_LINES` + clarification-rule shapes in `prompts.py`. Decidable offline — no
  LLM needed for the assertion (there is deliberately **no** fake confidence gate,
  because the dispatcher is text-in/text-out with no STT score).
- **Noise injectors** (`tester/noise.py`): pure, *seeded* messy-speech generation —
  disfluencies (fillers, repetitions, false starts) + ASR errors (homophones,
  number-word swaps, intermittent intent flip, dropped day-numbers, phone format
  drift), composed by `garble(text, seed, profile)`. Same input+seed → same output.

Run: `uv run pytest tester/ -q` (folded into `make verify`).

## Layer 4 — AI-driven live simulator (`tester/simulate.py`)

The "simulate calls without dialing" layer. An OpenAI LLM plays a **goal-seeking
caller** (persona + goal + stop condition, inventing each turn) against the **real
bot** (also OpenAI). A `RecordingBus` captures the call; `invariants.check_call`
audits it for the two hallucination invariants (unbacked outcome + spoken false
confirmation), and — for `noise_profile` personas — the clarification contract.

What it catches that scripts can't: emergent confusion, mid-call intent flips,
contradiction across turns, prompt-injection in a name field, demands for unoffered
slots, and **messy human speech** (every caller turn run through `garble` before the
bot hears it).

- **Curated personas** (`tester/personas.py`): confused-elderly, wrong-then-corrected
  DOB, cancel→reschedule flip, prompt-injection name, unoffered-slot demand,
  rude-but-completes, off-topic, hallucination-bait — plus **4 MESSY personas**
  (garbled numbers, intent-reversal trap, disfluent mind-changer, dropped day).
- **`--generate N`**: the LLM invents fresh adversarial personas at run time.
- **Honesty rule**: an adversarial caller *not getting its way is not a failure* —
  only a dishonest confirmation or a plow-ahead-on-garble is. Exit non-zero = a real
  bug caught. Generated personas that emit `[Placeholder]` text are flagged `corrupt`
  and excluded from the tally (data quality, not a bot failure).
- **Last live messy run**: 4/4 clean, **0 plowed_ahead_on_garble** — the bot fails
  safe under garble (asks to repeat rather than acting on a misheard value).

Run: `uv run python -m tester.simulate --only messy_intent_reversal` (needs
`OPENAI_API_KEY`) · `--generate 5` · `--concurrency 4` · `-v` for transcripts.
The pytest smoke test is gated on `PROSPER_EVAL_LIVE=1` so `make verify` stays free.

## Layer 4b — Live OpenAI eval (`make eval`)

The mock-eval scenarios re-run against the **real** OpenAI LLM (needs
`PROSPER_EVAL_LIVE=1` + key), with a real LLM judge. `make eval-baseline` exits `3`
if a previously-passing scenario regresses.

---

## How to run everything

```bash
make verify       # layers 1-3: ruff + format + mypy --strict + pytest (the pre-commit gate)
make mock-eval    # layer 2: 107 scenarios offline, ~5s, $0
make tester       # layer 3: the receipt/clarification gates
make simulate     # layer 4: live persona sim (needs OPENAI_API_KEY)
make eval         # layer 4b: live scenario eval (needs PROSPER_EVAL_LIVE=1 + key)
```

On Windows without `make`, invoke the underlying `uv run …` commands (see `Makefile`).
