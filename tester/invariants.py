"""Universal call invariants — checks that hold for *any* call, not a scenario.

A scripted scenario knows its expected DB delta; an autonomously-generated call
does not. So the simulator audits each call against invariants that must be true
regardless of what the caller asked for:

* **No unbacked outcome.** The terminal ``outcome`` event must have a matching
  successful write receipt (the tool-receipt gate, :mod:`tester.receipt_gate`).
* **No spoken hallucinated confirmation.** The bot must not *say* "I've
  cancelled / booked that" without a successful tool call behind it (reuses the
  deterministic regex already in ``evals.runner``).

Both are forms of *confirmed-but-didn't-happen* — the worst failure mode. An
adversarial caller failing to get what it wanted is NOT a violation (a bot that
refuses an injection or an invented slot is behaving correctly); only a
dishonest confirmation is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from evals.runner import _check_hallucinated_confirmation
from prosper.console.events import ConsoleEvent
from tester.receipt_gate import check_receipts


@dataclass(frozen=True, slots=True)
class InvariantViolation:
    """One invariant breach found while auditing a simulated call."""

    kind: str
    detail: str

    def __str__(self) -> str:
        """One-line rendering: ``kind: detail``."""
        return f"{self.kind}: {self.detail}"


def check_call(
    events: list[ConsoleEvent], transcript: list[dict[str, Any]]
) -> list[InvariantViolation]:
    """Audit a finished call; return every invariant breach (empty == honest)."""
    violations: list[InvariantViolation] = []
    for v in check_receipts(events):
        violations.append(InvariantViolation("unbacked_outcome", str(v)))
    for reason in _check_hallucinated_confirmation(transcript):
        violations.append(InvariantViolation("spoken_hallucination", reason))
    return violations
