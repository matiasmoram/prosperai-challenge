"""Scenario-from-transcript scaffolder (FUTURE.md 6.2).

Lowers the barrier to adding an eval scenario: paste a plain-text transcript
(``USER: ...`` / ``BOT: ...`` lines) and get a populated ``Scenario`` stub to
drop into ``evals/scenarios.py``. Fields that can't be inferred from text
(the DB ``setup`` callable, the precise persona wording) are emitted as
``# TODO`` markers.

Usage:
    uv run python scripts/scaffold_scenario.py < transcript.txt
    uv run python scripts/scaffold_scenario.py transcript.txt --name my_case

The heuristics are intentionally conservative — they over-emit ``# TODO``
rather than guess wrong, since a wrong ``StateExpectation`` makes a scenario
silently mis-assert.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class _Inferred:
    patient_delta: int
    active_delta: int
    cancelled_delta: int
    terminal_end: bool
    tool_codes: list[str]
    user_turns: int
    persona_lines: list[str]


_BOOKED_RE = re.compile(r"\b(booked|you'?re all set|all set for|see you (?:on|at))\b", re.I)
_CANCELLED_RE = re.compile(r"\b(cancelled|canceled)\b", re.I)
_REGISTERED_RE = re.compile(r"\b(registered|i'?ll register|set you up|created your)\b", re.I)
_RESCHEDULED_RE = re.compile(r"\b(rescheduled|moved your|moved to)\b", re.I)
_GOODBYE_RE = re.compile(r"\b(bye|goodbye|have a great day|take care)\b", re.I)


def _parse_lines(transcript: str) -> tuple[list[str], list[str]]:
    """Split a transcript into (user_lines, bot_lines), ignoring blanks/other."""
    user: list[str] = []
    bot: list[str] = []
    for raw in transcript.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("user:"):
            user.append(line.split(":", 1)[1].strip())
        elif low.startswith("bot:") or low.startswith("assistant:"):
            bot.append(line.split(":", 1)[1].strip())
    return user, bot


def _infer(transcript: str) -> _Inferred:
    user, bot = _parse_lines(transcript)
    bot_blob = "\n".join(bot)

    active_delta = 0
    cancelled_delta = 0
    patient_delta = 0
    tool_codes: list[str] = []

    if _BOOKED_RE.search(bot_blob):
        active_delta += 1
        tool_codes.append("create_appointment")
    if _CANCELLED_RE.search(bot_blob):
        cancelled_delta += 1
        active_delta -= 1
        tool_codes.append("cancel_appointment")
    if _RESCHEDULED_RE.search(bot_blob):
        # Atomic reschedule keeps the active count flat (same row, new slot).
        tool_codes.append("reschedule_appointment")
    if _REGISTERED_RE.search(bot_blob):
        patient_delta += 1
        tool_codes.append("create_patient")

    terminal_end = bool(_GOODBYE_RE.search(bot_blob)) or active_delta != 0 or cancelled_delta != 0
    return _Inferred(
        patient_delta=patient_delta,
        active_delta=active_delta,
        cancelled_delta=cancelled_delta,
        terminal_end=terminal_end,
        tool_codes=tool_codes,
        user_turns=len(user),
        persona_lines=user,
    )


def scaffold(transcript: str, *, name: str | None = None) -> str:
    """Return a paste-ready ``Scenario(...)`` stub inferred from a transcript."""
    inf = _infer(transcript)
    scenario_name = name or "TODO_scenario_name"
    max_turns = max(8, inf.user_turns + 4)

    persona_block = (
        " ".join(inf.persona_lines) if inf.persona_lines else "TODO: describe the caller"
    )
    tool_codes_repr = (
        "[\n" + "".join(f'                "{c}",\n' for c in inf.tool_codes) + "            ]"
        if inf.tool_codes
        else "[]  # TODO: list tools you expect to fire"
    )
    terminal = '"END"' if inf.terminal_end else "None  # TODO: confirm terminal state"

    return f'''    Scenario(
        name="{scenario_name}",
        tags=frozenset({{"TODO"}}),  # TODO: e.g. "happy" / "edge" / "adversarial"
        persona=(
            # TODO: rewrite as a tight caller persona with VERBATIM closers.
            "{persona_block}"
        ),
        setup=_TODO_setup,  # TODO: pick/define a DB seed (see existing _setup_* helpers)
        expected_state=StateExpectation(
            patient_count_delta={inf.patient_delta},
            active_appointment_count_delta={inf.active_delta},
            cancelled_appointment_count_delta={inf.cancelled_delta},
            expected_terminal_state={terminal},
            expected_tool_call_codes={tool_codes_repr},
        ),
        judge_criteria=[
            "TODO: 2-3 natural-language pass criteria a judge can check",
        ],
        max_turns={max_turns},
    ),'''


def main() -> int:
    parser = argparse.ArgumentParser(description="Scaffold a Scenario from a transcript.")
    parser.add_argument(
        "transcript",
        nargs="?",
        help="Path to a transcript file (USER:/BOT: lines). Reads stdin if omitted.",
    )
    parser.add_argument("--name", help="Scenario name to use in the stub.")
    args = parser.parse_args()

    text = (
        Path(args.transcript).read_text(encoding="utf-8") if args.transcript else sys.stdin.read()
    )

    if not text.strip():
        print(
            "ERROR: empty transcript (provide a file or pipe USER:/BOT: lines on stdin)",
            file=sys.stderr,
        )
        return 2

    print(scaffold(text, name=args.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
