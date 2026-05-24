.PHONY: install seed ehr bot dev test eval mock-eval trace eval-baseline lint type bench verify status pre-commit clean

install:
	uv sync

seed:
	uv run python scripts/seed.py

ehr:
	uv run uvicorn prosper.ehr.api:app --host 0.0.0.0 --port 8000 --reload

bot:
	uv run bot.py

dev:
	@echo "Run 'make seed' once, then 'make ehr' in one terminal and 'make bot' in another."

test:
	uv run pytest tests/ -v

eval:
	uv run pytest evals/test_scripted.py -v

# Run the full 16-scenario suite WITHOUT an OpenAI API key.
# Uses the deterministic mock LLM in evals/mock_llm.py. Finishes in
# ~5 s, great for routine verification.
mock-eval:
	uv run python -m evals --mock-llm

# Print a PII-redacted per-turn dispatcher trace for one scenario (FUTURE 6.1).
# Usage: make trace SCENARIO=new_patient_books
trace:
	uv run python -m evals --mock-llm --only $(SCENARIO) --trace

eval-baseline:
	uv run python -m evals --json evals/results/current.json --baseline evals/results/baseline.json

lint:
	uv run ruff check src/ tests/ evals/
	uv run ruff format --check src/ tests/ evals/

type:
	uv run mypy src/prosper

bench:
	uv run python scripts/bench.py --rounds 10

status:
	uv run python scripts/status.py

# One-shot pre-submit gate: everything CI runs, locally. Use this before
# pushing or opening a PR. Stops on first failure.
verify:
	@echo "==> lint" && uv run ruff check src/ tests/ evals/
	@echo "==> format" && uv run ruff format --check src/ tests/ evals/
	@echo "==> type" && uv run mypy src/prosper
	@echo "==> tests" && uv run pytest tests/ evals/test_types.py evals/test_runner_checks.py -q
	@echo "==> all green"

pre-commit:
	uv run pre-commit run --all-files

clean:
	rm -rf data/ .pytest_cache/ .mypy_cache/ .ruff_cache/ evals/results/
