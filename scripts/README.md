# scripts/

Standalone CLIs and dev utilities. Each script is self-contained and
runnable via `uv run python scripts/<name>.py`.

| Script | Purpose |
|---|---|
| `seed.py` | One-shot seed of the local SQLite EHR (3 providers, 14 days × 14 slots/day, 2 demo patients). Idempotent. |
| `bench.py` | Re-runnable benchmark of every read EHR endpoint. Prints min/p50/p95/max as a markdown table you can diff across runs to catch perf regressions. Usage: `uv run python scripts/bench.py --rounds 8 [--ehr-url http://127.0.0.1:8000]`. |

## How to use them in the dev loop

1. After any change to `src/prosper/ehr/`:
   ```bash
   uv run pytest tests/ehr/ -q          # correctness
   uv run python scripts/bench.py       # perf — paste output into the PR
   ```
2. After resetting `data/` (e.g. via `make clean`):
   ```bash
   uv run python scripts/seed.py
   ```
