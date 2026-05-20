"""Benchmark every EHR endpoint and print a markdown table.

Usage: ``uv run python scripts/bench.py [--ehr-url URL] [--rounds N]``

Default: hits the local EHR on http://127.0.0.1:8000, 5 rounds per endpoint,
reports min/p50/p95/max in ms. Designed to be re-runnable so you can pin a
"before" snapshot into git and diff against subsequent runs.

Does NOT need OpenAI keys or any LLM — purely the FastAPI EHR.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from datetime import date, timedelta

import httpx


async def _time_request(
    client: httpx.AsyncClient, method: str, path: str, **kwargs
) -> float:
    start = time.perf_counter()
    r = await client.request(method, path, **kwargs)
    r.raise_for_status() if r.status_code < 400 else None
    return (time.perf_counter() - start) * 1000


def _stats(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {"min": 0, "p50": 0, "p95": 0, "max": 0}
    s = sorted(samples)
    return {
        "min": s[0],
        "p50": statistics.median(s),
        "p95": s[max(0, int(0.95 * (len(s) - 1)))],
        "max": s[-1],
    }


async def run(base_url: str, rounds: int) -> None:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    cases: list[tuple[str, str, str, dict]] = [
        ("health", "GET", "/health", {}),
        ("availability", "GET", "/availability", {"params": {"date": tomorrow}}),
        (
            "by-phone (hit)",
            "GET",
            "/patients/by-phone",
            {"params": {"phone": "2025550100"}},
        ),
        (
            "by-phone (miss)",
            "GET",
            "/patients/by-phone",
            {"params": {"phone": "9999999999"}},
        ),
        (
            "by-name-dob",
            "GET",
            "/patients/by-name-dob",
            {"params": {"name": "Ada Lovelace", "dob": "1990-12-10"}},
        ),
    ]

    print(f"\n## EHR endpoint benchmark — {rounds} rounds against {base_url}\n")
    print("| endpoint | min | p50 | p95 | max |")
    print("|---|---|---|---|---|")
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
        # warm up once per case before measuring
        for _name, method, path, kwargs in cases:
            try:
                await client.request(method, path, **kwargs)
            except Exception:
                pass

        for name, method, path, kwargs in cases:
            samples = []
            for _ in range(rounds):
                try:
                    samples.append(await _time_request(client, method, path, **kwargs))
                except Exception as e:
                    print(f"| {name} | ERR | ERR | ERR | ERR | <!-- {e} -->")
                    samples = []
                    break
            if samples:
                s = _stats(samples)
                print(
                    f"| {name:18s} | {s['min']:5.1f} | {s['p50']:5.1f} | "
                    f"{s['p95']:5.1f} | {s['max']:5.1f} |"
                )

    print(f"\n_Run at {time.strftime('%Y-%m-%d %H:%M:%S')} UTC._\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ehr-url", default="http://127.0.0.1:8000")
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(run(args.ehr_url, args.rounds))


if __name__ == "__main__":
    main()
