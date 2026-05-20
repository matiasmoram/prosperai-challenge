"""CLI: ``python -m evals [--only NAME] [--tag TAG] [--json OUT.json]
[--baseline PREV.json] [--concurrency N] [-v]``.

Exits non-zero if any scenario fails OR if --baseline is supplied and at
least one scenario that previously passed now fails (regression).

Perf wave 2 #3: ``--concurrency N`` (default 4) runs scenarios under an
``asyncio.Semaphore`` so up to N LLM round-trips overlap. Each scenario
owns an isolated SQLite engine (see ``evals/runner._isolated_engine``),
so there's no shared mutable state between concurrent branches.
Set ``--concurrency 1`` for serial debug.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from openai import AsyncOpenAI

from evals.runner import run_scenario
from evals.scenarios import SCENARIOS
from evals.types import Scenario, ScenarioResult


def _select(args: argparse.Namespace) -> list[Scenario]:
    out = list(SCENARIOS)
    if args.only:
        out = [s for s in out if s.name in set(args.only)]
    if args.tag:
        wanted = set(args.tag)
        out = [s for s in out if not wanted.isdisjoint(s.tags)]
    return out


async def _run_all(scenarios: list[Scenario], *, concurrency: int) -> list[ScenarioResult]:
    """Run all selected scenarios with at most ``concurrency`` in flight.

    Each scenario builds its own SQLite engine + FastAPI app, so the only
    shared resources between branches are the AsyncOpenAI client (which
    httpx already serialises safely) and the semaphore itself.
    """
    client = AsyncOpenAI()
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _bounded(s: Scenario) -> ScenarioResult:
        async with sem:
            return await run_scenario(s, openai_client=client)

    return await asyncio.gather(*[_bounded(s) for s in scenarios])


def _summary(results: list[ScenarioResult]) -> str:
    lines = []
    for r in results:
        mark = "+" if r.overall_pass else "-"
        ttft = r.timing_summary.get("ttft", {}).get("p50", 0)
        cache_pct = r.cache_hit_ratio * 100
        lines.append(
            f"{mark} {r.name:42s} state={'P' if r.state_pass else 'F'} "
            f"judge={'P' if r.judge_pass else 'F'}  "
            f"turns={r.turns:2d}  total={r.duration_ms:6.0f}ms  "
            f"ttft_p50={ttft:5.0f}ms  cache={cache_pct:4.0f}%"
        )
        if not r.state_pass:
            for reason in r.state_reasons:
                lines.append(f"    state: {reason}")
        if not r.judge_pass:
            lines.append(f"    judge: {r.judge_justification.splitlines()[0]}")
    return "\n".join(lines)


def _to_json(results: list[ScenarioResult]) -> list[dict]:
    return [
        {
            "name": r.name,
            "overall": r.overall_pass,
            "state": r.state_pass,
            "state_reasons": r.state_reasons,
            "judge": r.judge_pass,
            "judge_justification": r.judge_justification,
            "turns": r.turns,
            "duration_ms": r.duration_ms,
            "timing_summary": r.timing_summary,
            "cached_prompt_tokens": r.cached_prompt_tokens,
            "prompt_tokens": r.prompt_tokens,
            "cache_hit_ratio": r.cache_hit_ratio,
        }
        for r in results
    ]


def _baseline_regressions(prev: list[dict], curr: list[ScenarioResult]) -> list[str]:
    prev_by_name = {p["name"]: p["overall"] for p in prev}
    regressions: list[str] = []
    for r in curr:
        if prev_by_name.get(r.name) and not r.overall_pass:
            regressions.append(r.name)
    return regressions


def _print_aggregate_latency(results: list[ScenarioResult]) -> None:
    agg: dict[str, list[float]] = defaultdict(list)
    for r in results:
        for phase, stats in r.timing_summary.items():
            agg[phase].extend([stats["p50"]] * int(stats["count"]))
    if agg:
        print("\nLatency p50 across all scenarios (ms):")
        for phase in sorted(agg):
            vals = sorted(agg[phase])
            p50 = vals[len(vals) // 2]
            print(f"  {phase:40s} p50={p50:.0f}  n={len(vals)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", action="append")
    parser.add_argument("--tag", action="append")
    parser.add_argument("--json")
    parser.add_argument("--baseline")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Max scenarios in flight (default 4). Use 1 for serial debug.",
    )
    parser.add_argument("-v", action="store_true")
    args = parser.parse_args()

    if args.concurrency < 1:
        print("ERROR: --concurrency must be >= 1", file=sys.stderr)
        return 2

    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        return 2

    scenarios = _select(args)
    if not scenarios:
        print("no scenarios selected", file=sys.stderr)
        return 2

    results = asyncio.run(_run_all(scenarios))
    print(_summary(results))
    _print_aggregate_latency(results)

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(_to_json(results), indent=2))

    if args.baseline and Path(args.baseline).exists():
        prev = json.loads(Path(args.baseline).read_text())
        regressions = _baseline_regressions(prev, results)
        if regressions:
            print(f"\nREGRESSIONS vs baseline: {regressions}", file=sys.stderr)
            return 3

    return 0 if all(r.overall_pass for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
