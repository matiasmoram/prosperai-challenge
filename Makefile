.PHONY: install seed ehr bot run-all dev test eval mock-eval trace replay replay-record gen-list gen-eval eval-baseline tester lint type bench verify status pre-commit clean

install:
	uv sync

seed:
	uv run python scripts/seed.py

ehr:
	uv run uvicorn prosper.ehr.api:app --host 0.0.0.0 --port 8000 --reload

bot:
	uv run bot.py

# One command, every process + surface (EHR :8000, bot :7860, console+call+frontdesk :7861).
run-all:
	uv run python scripts/run_all.py

dev:
	@echo "Run 'make seed' once, then 'make ehr' in one terminal and 'make bot' in another."

test:
	uv run pytest tests/ -v

eval:
	uv run pytest evals/test_scripted.py -v

# Real acoustic TTS->STT round-trip via ElevenLabs (spends credits). Double-gated
# so it never runs in verify/test/pre-commit. Needs ELEVENLABS_API_KEY (.env) and
# the explicit PROSPER_AUDIO_LIVE=1 opt-in (set here).
audio-smoke:
	PROSPER_AUDIO_LIVE=1 uv run pytest evals/audio_smoke -v

# Run the full 16-scenario suite WITHOUT an OpenAI API key.
# Uses the deterministic mock LLM in evals/mock_llm.py. Finishes in
# ~5 s, great for routine verification.
mock-eval:
	uv run python -m evals --mock-llm

# Print a PII-redacted per-turn dispatcher trace for one scenario (FUTURE 6.1).
# Usage: make trace SCENARIO=new_patient_books
trace:
	uv run python -m evals --mock-llm --only $(SCENARIO) --trace

# Golden-trace replay: order-sensitive FSM transition/tool regression guard
# (FUTURE 2.3). Zero tokens. `make replay-record` to regenerate goldens.
replay:
	uv run python -m evals.trace_replay
replay-record:
	uv run python -m evals.trace_replay --record

# Adversarial scenario generator (FUTURE 2.1). `gen-list` previews the
# generated variants offline; `gen-eval` runs them against the live LLM.
gen-list:
	uv run python -m evals.generator --list
gen-eval:
	uv run python -m evals.generator

eval-baseline:
	uv run python -m evals --json evals/results/current.json --baseline evals/results/baseline.json

# Prototype harnesses that catch agent mistakes without hand-dialing
# (tool-receipt hallucination gate). Offline, zero tokens.
tester:
	uv run pytest tester/ -v

# Autonomous adversarial call simulator: the LLM plays goal-seeking callers
# against the real bot (no scripted turns, no human dialing), and fails if any
# call confirms something it didn't do. Live — needs OPENAI_API_KEY (.env).
# Usage: make simulate            (all curated personas)
#        make simulate ARGS="--generate 5 -v"
simulate:
	uv run python -m tester.simulate $(ARGS)

lint:
	uv run ruff check src/ tests/ evals/ tester/
	uv run ruff format --check src/ tests/ evals/ tester/

type:
	uv run mypy src/prosper

bench:
	uv run python scripts/bench.py --rounds 10

status:
	uv run python scripts/status.py

# One-shot pre-submit gate: everything CI runs, locally. Use this before
# pushing or opening a PR. Stops on first failure.
verify:
	@echo "==> lint" && uv run ruff check src/ tests/ evals/ tester/
	@echo "==> format" && uv run ruff format --check src/ tests/ evals/ tester/
	@echo "==> type" && uv run mypy src/prosper
	@echo "==> tests" && uv run pytest tests/ evals/test_types.py evals/test_runner_checks.py tester/ -q
	@echo "==> all green"

pre-commit:
	uv run pre-commit run --all-files

clean:
	rm -rf data/ .pytest_cache/ .mypy_cache/ .ruff_cache/ evals/results/
