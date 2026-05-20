"""In-process latency collector — phase → list of durations → p50/p95/count.

Designed for short-lived sessions (one call). The dispatcher calls
``record`` after each LLM/tool/EHR span and ``format_table`` at session end.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from statistics import median
from typing import AsyncIterator


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = int(round((pct / 100) * (len(s) - 1)))
    return s[max(0, min(len(s) - 1, k))]


class TimingCollector:
    def __init__(self) -> None:
        self._spans: dict[str, list[float]] = defaultdict(list)

    def record(self, *, phase: str, duration_ms: float, state: str) -> None:
        self._spans[phase].append(duration_ms)
        print(
            json.dumps(
                {
                    "evt": "span",
                    "phase": phase,
                    "state": state,
                    "duration_ms": round(duration_ms, 2),
                }
            ),
            flush=True,
        )

    @asynccontextmanager
    async def measure(self, *, phase: str, state: str) -> AsyncIterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(
                phase=phase,
                duration_ms=(time.perf_counter() - start) * 1000,
                state=state,
            )

    def summary(self) -> dict[str, dict[str, float]]:
        return {
            phase: {
                "count": len(values),
                "p50": median(values),
                "p95": _percentile(values, 95),
                "max": max(values),
            }
            for phase, values in self._spans.items()
        }

    def format_table(self) -> str:
        rows = ["phase                                count    p50ms    p95ms    maxms"]
        for phase, stats in sorted(self.summary().items()):
            rows.append(
                f"{phase[:36]:36s} {int(stats['count']):>5d} "
                f"{stats['p50']:>8.0f} {stats['p95']:>8.0f} {stats['max']:>8.0f}"
            )
        return "\n".join(rows)
