"""The clarification contract: did the bot recover from garbled input?

When a caller utterance is garbled or under-specified (see :mod:`tester.noise`),
a well-behaved bot must NOT silently act on the misheard value. It must instead
ask the caller to repeat / confirm, or simply not advance. This module turns
that into a decidable, offline check — no LLM needed for the core assertion.

The behaviour itself lives in ``prompts.py`` (the clarification rule at ~line
159 + ``FALLBACK_LINES``); there is deliberately **no** dispatcher confidence
gate, because our dispatcher is text-in/text-out and has no STT confidence score
to gate on — a numeric gate would be fake. So we *assert* the behaviour here
rather than encode it as new product code.

CONTRACT — on a garbled/under-specified caller turn, before any write tool
(``create_appointment`` / ``cancel_appointment`` / ``reschedule_appointment``)
consumes that turn's value, the bot must have either (a) asked a
clarification/confirmation question or (b) not advanced. A write that lands with
no intervening clarification is the violation ``plowed_ahead_on_garble``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from tester.invariants import InvariantViolation

# Write tools — a positive one of these consuming a garbled value with no
# preceding clarification is the failure this module exists to catch.
_WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {"create_appointment", "cancel_appointment", "reschedule_appointment"}
)

# Clarification / re-prompt / read-back SHAPES. Anchored to the actual copy in
# prompts.py FALLBACK_LINES ("could you repeat that?", "I didn't catch that —
# could you say it again?") + the clarification rule ("ask one short clarifying
# question"), kept as shapes so wording can vary without breaking the detector.
_CLARIFY_RE: Final[re.Pattern[str]] = re.compile(
    r"repeat that"
    r"|say (?:that )?again"
    r"|didn'?t (?:catch|get)"
    r"|could you (?:repeat|confirm|say|clarify|spell)"
    r"|can you (?:repeat|confirm|say|clarify|spell)"
    r"|which (?:day|time|appointment|one|date|provider)"
    r"|just to (?:confirm|make sure|double[- ]?check)"
    r"|did you (?:say|mean)"
    r"|i lost track"
    r"|i'?m not (?:sure|certain) (?:what|which|i)"
    r"|sorry,? (?:i|could|what|can)"
    r"|to confirm[, ]"
    r"|let me make sure",
    re.IGNORECASE,
)


def detect_clarification(bot_text: str) -> bool:
    """True if ``bot_text`` reads as a clarification / re-prompt / read-back."""
    return _CLARIFY_RE.search(bot_text) is not None


@dataclass(frozen=True, slots=True)
class Turn:
    """One normalized turn in a simulated call, for the clarification audit.

    ``garbled`` marks a caller turn whose text was run through
    :func:`tester.noise.garble`; ``wrote`` names the write tool (if any) the bot
    fired while producing a bot turn. The live driver fills these from its loop
    + the recorded console events.
    """

    role: str  # "caller" | "bot"
    text: str
    garbled: bool = False
    original: str | None = None
    wrote: str | None = None


@dataclass
class _PendingGarble:
    """A garbled caller turn not yet cleared by a clarification."""

    text: str
    original: str | None = None


def check_recovers_gracefully(turns: list[Turn]) -> list[InvariantViolation]:
    """Audit a call for ``plowed_ahead_on_garble`` — return every breach.

    Walk the turns in order. A garbled caller turn becomes *pending*. A
    subsequent bot clarification/confirmation *clears* the pending garble (the
    bot did the right thing). A write tool firing while a garble is still pending
    is a violation: the bot committed on a value it should have re-checked.

    A confirm that clears one garble before a later garble is tracked correctly
    because only the most recent uncleared garble is pending at any point.
    """
    violations: list[InvariantViolation] = []
    pending: _PendingGarble | None = None
    for t in turns:
        if t.role == "caller":
            if t.garbled:
                pending = _PendingGarble(text=t.text, original=t.original)
        elif t.role == "bot":
            if pending is not None and detect_clarification(t.text):
                pending = None
                continue
            if t.wrote in _WRITE_TOOLS and pending is not None:
                violations.append(
                    InvariantViolation(
                        "plowed_ahead_on_garble",
                        f"{t.wrote} fired on garbled input "
                        f"(heard {pending.text!r}, said {pending.original!r}) "
                        f"with no clarification or confirmation first",
                    )
                )
                pending = None
    return violations


@dataclass(frozen=True, slots=True)
class GarbleLog:
    """Record of which caller turns the driver garbled, by 0-based caller index."""

    by_index: dict[int, tuple[str, str]] = field(default_factory=dict)  # idx -> (original, garbled)

    def mark(self, caller_index: int, original: str, garbled: str) -> None:
        """Record that caller turn ``caller_index`` was garbled."""
        self.by_index[caller_index] = (original, garbled)
