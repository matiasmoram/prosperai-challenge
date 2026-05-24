# Known errors / open issues

Honest snapshot of what's broken right now. Schema-level crashes are **bugs**;
state-assertion / judge failures under the **live** LLM are usually **scope
mismatches** between the scenario's persona script and the bot's real
conversational style, not data corruption.

**Two kinds of truth in this file, kept separate on purpose:**

- **Verified now** — offline checks re-run this session (no API key needed).
- **Unverified (live)** — last measured against real OpenAI on 2026-05-20;
  **not re-run since.** Needs `PROSPER_EVAL_LIVE=1` + `OPENAI_API_KEY` + saldo
  (`make eval`) to get a current number. Treat as stale until then.

Last refreshed: **2026-05-24**.

---

## Verified now (offline, this session)

Re-run with `make verify` + `make mock-eval`. All green:

- **456 tests pass**, 0 failed (`uv run pytest tests/ -q`, ~16 s).
- **`make mock-eval`: 55/55 scenarios** `state=P judge=P`, 0 failing (~5 s, no API key).
- `mypy --strict` clean; ruff check + format clean (via `make verify`).
- EHR endpoints 200, p50 < 100 ms warm; `/metrics`, `/health`, `/docs` live.
- No data corruption, no hallucinated appointments, no breakage under the
  security/concurrency/adversarial suites (`tests/adversarial/`, see
  `docs/testing/ADVERSARIAL_FINDINGS.md` — F-001…F-012 all fixed at root).

**Caveat:** `make mock-eval` runs *canned* scripts through `evals/mock_llm.py`.
Green mock ≠ green live. The failures in the next section were real-LLM
behaviours; passing them offline does **not** prove they're fixed live.

---

## Unverified (live) — last measured 2026-05-20, NOT re-run since

This whole section is frozen at commit `18bbe28` (2026-05-20), **before** the
Phase 7–9 work (id-contract fix, goodbye→END transition, +37 mock scenarios,
adversarial hardening). Several root causes below have since had code land +
offline regression tests; whether that closes the *live* failure is unconfirmed
until someone runs `make eval`.

### E1. `patient_correction_mid_register` — OpenAI 400 on messages[2].role

**Status:** UNVERIFIED. Was a real live crash on 2026-05-20; offline the
scenario now passes in `make mock-eval`. Phase 7 (`30bda0f`) added
history-orphan pruning for the OpenAI-400 class, which *may* cover this — but
the mock can't reproduce the live message ordering, so this is **not confirmed
fixed**. Re-run live to settle it.

**Original symptom:**
```
BadRequestError: Error code: 400
'Invalid parameter: messages with role tool must be a response to a
preceeding message with tool_calls'  param=messages.[2].role
```

**Repro (needs live):**
```bash
PROSPER_EVAL_LIVE=1 OPENAI_API_KEY=... uv run python -m evals \
    --only patient_correction_mid_register
```

### Live scenario state-assertion failures (2026-05-20 snapshot)

On 2026-05-20, 13–15 scenarios reached the wrong terminal state under the live
LLM. The dominant root cause was **"no universal goodbye→END transition"** —
the bot finished its job but the persona's "bye" never moved the FSM to `END`.

**Code status now (verified offline):** that transition **exists** —
`flows.py` has `"goodbye": State.END` on every state and `dispatcher.py` has a
universal goodbye intent (`_GOODBYE_HARD` / tiered matchers).
`tests/adversarial/test_confirm_goodbye.py` +
`test_goodbye_false_positive.py` regression-guard both the true-positive and
false-positive directions. So the *class* of failure is addressed at root.

**What's still unknown:** the actual live pass-rate. The per-scenario table
that used to live here (15 rows) was a 2026-05-20 artifact and is no longer
accurate — the scenarios and the bot have both changed. Rather than keep a
stale table, the honest statement is: **live pass-rate has not been
re-measured.** Run `make eval` (or `make eval-baseline`) to regenerate it.

---

## How to refresh this file

1. Offline (cheap, do anytime): `make verify` + `make mock-eval`. Update the
   "Verified now" counts + date.
2. Live (needs key + saldo): `make eval`. If E1 no longer crashes and the
   terminal-state failures are gone, delete the "Unverified (live)" section.
   If new failures appear, document each with symptom + root cause + repro.
