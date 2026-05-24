"""Tool-receipt hallucination gate — a standing invariant over the console bus.

The voice agent's worst failure mode is *confirmed-but-didn't-happen*: it tells
the caller "you're booked for Tuesday 3pm" when ``create_appointment`` actually
returned an ``Err``, or the slot/appointment id was invented. The dispatcher
already blocks the most direct routes at runtime (memory-handle resolution, the
``hallucinated_slot_id`` / ``hallucinated_appointment_id`` guards). What was
missing is an *after-the-fact audit* proving the agent never claimed a write
that has no successful tool behind it.

This gate does that, structurally, over the operator-console event stream
(:mod:`prosper.console`). It is the NABAOS "tool receipt" pattern reduced to
its smallest useful form:

* The terminal ``outcome`` event is a **claim** — ``booked`` / ``cancelled`` /
  ``rescheduled`` each assert that a write succeeded.
* Each ``tool_call_end`` event with ``outcome == "ok"`` is a **receipt** — a
  write the dispatcher actually completed.

A positive claim with no matching receipt earlier in the same session is a
:class:`Violation`. ``refused`` / ``abandoned`` make no positive claim, so they
require no receipt.

Scope note: this guards the FSM's own outcome accounting, *not* the spoken
words — the spoken-claim regex in
``evals.runner._check_hallucinated_confirmation`` already covers transcript
text. The two are complementary; see ``tester/README.md``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from prosper.console.events import ConsoleEvent

# A positive outcome label -> the write tool whose success is its receipt.
# Mirrors the FSM edges in `dispatcher._maybe_transition_from_tool`:
#   CONFIRM_BOOK       + create_appointment ok     -> "booked"
#   CONFIRM_CANCEL     + cancel_appointment ok      -> "cancelled"
#   CONFIRM_RESCHEDULE + reschedule_appointment ok  -> "rescheduled"
# The cancel-then-rebook chain also ends in "booked" via a create_appointment
# success, so a single create receipt covers it too.
RECEIPT_REQUIRED: dict[str, str] = {
    "booked": "create_appointment",
    "cancelled": "cancel_appointment",
    "rescheduled": "reschedule_appointment",
}

# Outcomes that assert nothing happened -> no receipt required.
NO_CLAIM: frozenset[str] = frozenset({"refused", "abandoned"})


@dataclass(frozen=True, slots=True)
class Violation:
    """One unbacked outcome claim found in a session's event stream."""

    session_id: str
    outcome: str
    expected_tool: str
    detail: str

    def __str__(self) -> str:
        """One-line, log-friendly rendering keyed by session id."""
        return f"[{self.session_id}] {self.detail}"


def successful_write_tools(events: Iterable[ConsoleEvent]) -> set[str]:
    """Return the set of tool names that completed with ``outcome == "ok"``."""
    return {
        str(e.payload.get("tool", ""))
        for e in events
        if e.type == "tool_call_end" and e.payload.get("outcome") == "ok"
    }


def check_receipts(events: Sequence[ConsoleEvent]) -> list[Violation]:
    """Audit an event stream; return every outcome claim lacking its receipt.

    Events are grouped by ``session_id`` and scanned in arrival order, so a
    receipt only counts when it precedes the claim within the *same* session
    (a success in one call can never vouch for another call's confirmation).
    An empty list means every positive outcome was backed by a successful
    write — the agent confirmed nothing it didn't do.
    """
    by_session: dict[str, list[ConsoleEvent]] = defaultdict(list)
    for e in events:
        by_session[e.session_id].append(e)

    violations: list[Violation] = []
    for session_id, session_events in by_session.items():
        seen_writes: set[str] = set()
        for e in session_events:
            if e.type == "tool_call_end" and e.payload.get("outcome") == "ok":
                seen_writes.add(str(e.payload.get("tool", "")))
            elif e.type == "outcome":
                outcome = str(e.payload.get("outcome", ""))
                if outcome in NO_CLAIM:
                    continue
                required = RECEIPT_REQUIRED.get(outcome)
                if required is None:
                    violations.append(
                        Violation(
                            session_id=session_id,
                            outcome=outcome,
                            expected_tool="",
                            detail=f"unknown outcome label {outcome!r} — no receipt rule",
                        )
                    )
                elif required not in seen_writes:
                    violations.append(
                        Violation(
                            session_id=session_id,
                            outcome=outcome,
                            expected_tool=required,
                            detail=(
                                f"outcome={outcome!r} claimed but no successful "
                                f"{required} receipt earlier in session"
                            ),
                        )
                    )
    return violations
