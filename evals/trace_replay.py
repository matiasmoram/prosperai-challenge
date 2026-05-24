"""Golden-trace replay (FUTURE.md 2.3): deterministic, zero-token regression.

The mock-eval suite (``python -m evals --mock-llm``) asserts END-state deltas
and *tool presence*. This module adds a stricter, complementary guard: it
captures the **ordered fingerprint** of a scenario's FSM transitions and tool
outcomes and compares it byte-for-byte against a stored golden. A reordered
transition, a dropped tool call, or a new spurious tool_rejected — none of
which a delta-only check would catch — fails the replay.

It reuses the existing mock runner, so it needs no API key and runs in well
under a second.

Usage:
    uv run python -m evals.trace_replay            # replay all goldens, diff
    uv run python -m evals.trace_replay --record   # (re)write goldens from
                                                    # current behaviour
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from evals.runner import run_scenario
from evals.scenarios import SCENARIOS

_TRACES_DIR = Path(__file__).parent / "traces"

# Scenarios whose transition/tool order we pin as golden traces. Chosen to
# cover distinct FSM paths: new-patient register+book, single cancel,
# pick-from-list cancel, and the atomic reschedule.
GOLDEN_SCENARIOS: tuple[str, ...] = (
    "new_patient_books",
    "existing_patient_cancels",
    "cancel_picks_from_list",
    "reschedule_existing_appointment",
)


def transcript_fingerprint(transcript: list[dict]) -> list[str]:
    """Reduce a dispatcher transcript to an ordered, PII-free fingerprint.

    Only the structural events that define the call's control flow are
    kept — FSM transitions and tool outcomes. Spoken text (which varies
    with prompt wording and would carry PII) is intentionally excluded, so
    the fingerprint is stable across prompt copy-edits but sensitive to any
    change in *what the FSM did*.
    """
    fp: list[str] = []
    for ev in transcript:
        kind = ev.get("kind")
        if kind == "transition":
            fp.append(f"T {ev['from']}->{ev['to']} {ev['label']}")
        elif kind == "tool_ok":
            fp.append(f"OK {ev['name']}")
        elif kind == "tool_err":
            fp.append(f"ERR {ev['name']} {ev['code']}")
        elif kind == "tool_rejected":
            fp.append(f"REJ {ev['name']}")
    return fp


async def fingerprint_for(name: str) -> list[str]:
    """Run one scenario through the mock runner and return its fingerprint."""
    scenario = next(s for s in SCENARIOS if s.name == name)
    result = await run_scenario(scenario, mock=True)
    return transcript_fingerprint(result.transcript)


def _golden_path(name: str) -> Path:
    return _TRACES_DIR / f"{name}.json"


def load_golden(name: str) -> list[str]:
    return json.loads(_golden_path(name).read_text())


async def record(names: tuple[str, ...]) -> None:
    """(Re)write golden fingerprints for ``names`` from current behaviour."""
    _TRACES_DIR.mkdir(parents=True, exist_ok=True)
    for name in names:
        fp = await fingerprint_for(name)
        _golden_path(name).write_text(json.dumps(fp, indent=2) + "\n")
        print(f"recorded {name}: {len(fp)} steps -> {_golden_path(name).name}")


def _diff(golden: list[str], actual: list[str]) -> list[str]:
    """Return human-readable diff lines; empty list means identical."""
    if golden == actual:
        return []
    lines = ["  golden vs actual:"]
    for i in range(max(len(golden), len(actual))):
        g = golden[i] if i < len(golden) else "<missing>"
        a = actual[i] if i < len(actual) else "<missing>"
        mark = " " if g == a else ">"
        lines.append(f"  {mark} [{i}] {g!r}  |  {a!r}")
    return lines


async def replay(names: tuple[str, ...]) -> int:
    """Replay each golden trace; print PASS/FAIL + diffs. Return exit code."""
    failures = 0
    for name in names:
        golden_file = _golden_path(name)
        if not golden_file.exists():
            print(f"- {name}: NO GOLDEN (run --record first)")
            failures += 1
            continue
        golden = load_golden(name)
        actual = await fingerprint_for(name)
        diff = _diff(golden, actual)
        if diff:
            failures += 1
            print(f"- {name}: FAIL ({len(golden)} golden vs {len(actual)} actual steps)")
            print("\n".join(diff))
        else:
            print(f"+ {name}: ok ({len(golden)} steps)")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Golden-trace replay (FUTURE 2.3).")
    parser.add_argument(
        "--record",
        action="store_true",
        help="(Re)write golden fingerprints from current behaviour instead of checking.",
    )
    parser.add_argument(
        "--only",
        action="append",
        help="Limit to specific scenario name(s); repeatable.",
    )
    args = parser.parse_args()
    names = tuple(args.only) if args.only else GOLDEN_SCENARIOS
    if args.record:
        asyncio.run(record(names))
        return 0
    return asyncio.run(replay(names))


if __name__ == "__main__":
    raise SystemExit(main())
