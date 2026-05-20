.PHONY: install seed ehr bot dev test eval eval-baseline lint type bench pre-commit clean

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

eval-baseline:
	uv run python -m evals --json evals/results/current.json --baseline evals/results/baseline.json

lint:
	uv run ruff check src/ tests/ evals/
	uv run ruff format --check src/ tests/ evals/

type:
	uv run mypy src/prosper

bench:
	uv run python scripts/bench.py --rounds 10

pre-commit:
	uv run pre-commit run --all-files

clean:
	rm -rf data/ .pytest_cache/ .mypy_cache/ .ruff_cache/ evals/results/
