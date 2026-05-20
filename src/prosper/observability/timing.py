"""In-process latency collector — phase → list of durations → p50/p95/count.

Designed for short-lived sessions (one call). The dispatcher calls
``record`` after each LLM/tool/EHR span and ``format_table`` at session end.

Each span emits a structured JSON log line:

    {"evt":"span","phase":"llm","state":"GREETING","duration_ms":12.3,
     "session_id":"<uuid>","turn_id":3}

``session_id`` / ``turn_id`` are optional — when absent the keys are
omitted so older grep recipes keep working. They are populated by the
dispatcher (which knows the per-call uuid + auto-incrementing turn
counter) before forwarding the call to :meth:`TimingCollector.record`.
The bot's ``DispatcherProcessor`` also passes them when emitting TTFT
spans so every line on stderr is joinable on (session_id, turn_id).
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from statistics import median


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = round((pct / 100) * (len(s) - 1))
    return s[max(0, min(len(s) - 1, k))]


class TimingCollector:
    def __init__(self) -> None:
        self._spans: dict[str, list[float]] = defaultdict(list)

    def record(
        self,
        *,
        phase: str,
        duration_ms: float,
        state: str,
        session_id: str | None = None,
        turn_id: int | None = None,
    ) -> None:
        self._spans[phase].append(duration_ms)
        payload: dict[str, object] = {
            "evt": "span",
            "phase": phase,
            "state": state,
            "duration_ms": round(duration_ms, 2),
        }
        if session_id is not None:
            payload["session_id"] = session_id
        if turn_id is not None:
            payload["turn_id"] = turn_id
        print(json.dumps(payload), flush=True)

    @asynccontextmanager
    async def measure(
        self,
        *,
        phase: str,
        state: str,
        session_id: str | None = None,
        turn_id: int | None = None,
    ) -> AsyncIterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(
                phase=phase,
                duration_ms=(time.perf_counter() - start) * 1000,
                state=state,
                session_id=session_id,
                turn_id=turn_id,
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
