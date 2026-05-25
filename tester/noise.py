"""Deterministic messy-human noise injection for caller utterances.

Real callers don't speak in clean sentences and real speech-to-text mishears
what they say. These pure, *seeded* transformers turn a clean caller utterance
into a realistically messy one so the simulator can check the bot degrades
gracefully — asks the caller to repeat / confirm rather than plowing ahead on a
misheard value (see :mod:`tester.clarification`).

Two independent noise sources, composed by :func:`garble`:

* **disfluencies** — how humans actually talk: fillers ("um"), word
  repetitions ("the, the"), false starts ("I— I want"). Loosely follows
  Shriberg's reparandum/interregnum/repair model (arXiv:2201.05041).
* **ASR errors** — how speech-to-text mishears: homophone swaps, number-word
  swaps (fifteen↔fifty), the dangerous intent-word flip (cancel↔schedule),
  dropped day-numbers, and phone/number formatting drift. Modelled on the
  production voice-agent failure-mode taxonomy (hamming.ai).

Everything is seeded: ``garble(text, seed=…, profile=…)`` is a pure function —
the same input and seed always yield the same output, so a failing sim run is
reproducible and the unit tests are stable. No I/O, no LLM, no global ``random``.
"""

from __future__ import annotations

import random
import re
from typing import Final

# --- ASR confusion tables ---------------------------------------------------
# Homophones a real STT routinely swaps. Bidirectional pairs are expanded to
# both directions at import time so injection can fire on either spelling.
_HOMOPHONE_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    ("to", "two"),
    ("too", "two"),
    ("for", "four"),
    ("ate", "eight"),
    ("there", "their"),
    ("here", "hear"),
    ("no", "know"),
    ("right", "write"),
    ("by", "buy"),
    ("one", "won"),
    ("week", "weak"),
    ("knight", "night"),
    ("sun", "son"),
    ("would", "wood"),
)
_HOMOPHONES: Final[dict[str, str]] = {a: b for a, b in _HOMOPHONE_PAIRS}

# Number words a real STT swaps because they are near-homophones over the phone
# ("fifteen" / "fifty"). The single most common date/time mis-transcription.
_NUMBER_SWAPS: Final[dict[str, str]] = {
    "thirteen": "thirty",
    "thirty": "thirteen",
    "fourteen": "forty",
    "forty": "fourteen",
    "fifteen": "fifty",
    "fifty": "fifteen",
    "sixteen": "sixty",
    "sixty": "sixteen",
    "seventeen": "seventy",
    "seventy": "seventeen",
    "eighteen": "eighty",
    "eighty": "eighteen",
    "nineteen": "ninety",
    "ninety": "nineteen",
}

# The DANGEROUS flip: STT (or a noisy line) turns one intent verb into another.
# A bot that acts on the misheard intent without confirming is the failure this
# whole suite exists to catch, so these are kept separate from benign homophones.
_INTENT_FLIPS: Final[dict[str, str]] = {
    "cancel": "schedule",
    "cancelling": "scheduling",
    "reschedule": "cancel",
    "book": "cancel",
    "booking": "cancelling",
}

_FILLERS: Final[tuple[str, ...]] = ("um", "uh", "like", "you know", "er")

# A month name with no following day-number → STT dropped the day.
_MONTHS: Final[str] = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
)
_MONTH_DAY_RE: Final[re.Pattern[str]] = re.compile(
    rf"\b({_MONTHS})\s+(\d{{1,2}})(st|nd|rd|th)?\b", re.IGNORECASE
)
_PHONE_RE: Final[re.Pattern[str]] = re.compile(r"\b(\d{3})[-.\s](\d{3})[-.\s](\d{4})\b")
_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z']+")

# Which disfluency / ASR knobs each named profile turns on.
_PROFILES: Final[dict[str, dict[str, object]]] = {
    "light": {"disfluency": {"filler", "repetition"}, "asr": {"homophone"}, "rate": 0.2},
    "heavy": {
        "disfluency": {"filler", "repetition", "restart"},
        "asr": {"homophone", "number_swap", "format_drift"},
        "rate": 0.5,
    },
    "asr_only": {"disfluency": set(), "asr": {"homophone", "number_swap"}, "rate": 0.6},
    "disfluent_only": {
        "disfluency": {"filler", "repetition", "restart"},
        "asr": set(),
        "rate": 0.4,
    },
    "number_garble": {"disfluency": set(), "asr": {"number_swap", "number_drop"}, "rate": 1.0},
    "intent_reversal": {"disfluency": set(), "asr": {"intent_flip"}, "rate": 1.0},
}


def inject_disfluencies(text: str, *, rng: random.Random, types: set[str], rate: float) -> str:
    """Insert human disfluencies into ``text`` at the given ``rate`` (0..1).

    ``types`` ⊆ {"filler", "repetition", "restart"}. Deterministic given ``rng``.
    Token order and the original words are preserved — disfluencies are *added*,
    never substituted, so the underlying meaning still survives for the bot.
    """
    if not types or rate <= 0:
        return text
    tokens = text.split()
    if not tokens:
        return text
    out: list[str] = []
    for i, tok in enumerate(tokens):
        if "filler" in types and rng.random() < rate / 2:
            out.append(rng.choice(_FILLERS))
        if "restart" in types and i == 0 and len(tok) > 1 and rng.random() < rate:
            # False start: emit the first letter as a cut-off word, then restart.
            out.append(f"{tok[0].lower()}—")
        out.append(tok)
        if "repetition" in types and rng.random() < rate / 2:
            out.append(tok)
    return " ".join(out)


def _swap_word(word: str, table: dict[str, str]) -> str | None:
    """Case-preserving lookup of ``word`` in ``table``; None if no entry."""
    repl = table.get(word.lower())
    if repl is None:
        return None
    if word.istitle():
        return repl.title()
    if word.isupper():
        return repl.upper()
    return repl


def inject_asr_errors(text: str, *, rng: random.Random, modes: set[str]) -> str:
    """Apply ASR-style mishearings to ``text``. Deterministic given ``rng``.

    ``modes`` ⊆ {"homophone", "number_swap", "intent_flip", "number_drop",
    "format_drift"}. ``number_drop`` and ``format_drift`` are structural (regex
    over the whole string); the rest are per-word substitutions.
    """
    result = text
    if "number_drop" in modes:
        # "December 15th" → "December" (STT dropped the day-number).
        result = _MONTH_DAY_RE.sub(lambda m: m.group(1), result)
    if "format_drift" in modes:
        # "555-0142" / "202 555 0100" → run-on digits.
        result = _PHONE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}", result)

    word_tables: list[dict[str, str]] = []
    if "intent_flip" in modes:
        word_tables.append(_INTENT_FLIPS)
    if "number_swap" in modes:
        word_tables.append(_NUMBER_SWAPS)
    if "homophone" in modes:
        word_tables.append(_HOMOPHONES)
    if not word_tables:
        return result

    def _sub(match: re.Match[str]) -> str:
        word = match.group(0)
        for table in word_tables:
            swapped = _swap_word(word, table)
            # All swaps (incl. the dangerous intent flip) fire probabilistically.
            # Intermittence is deliberate: real ASR is not 100% wrong, and a
            # caller whose "cancel" is *always* flipped can never recover — so an
            # intermittent flip tests both safety (don't act on a flipped turn)
            # AND recovery (converge when a clean turn lands), seeded per turn.
            if swapped is not None and rng.random() < 0.7:
                return swapped
        return word

    return _WORD_RE.sub(_sub, result)


def garble(text: str, *, seed: int, profile: str = "light") -> str:
    """Return a messy version of ``text`` per the named ``profile``.

    Pure + seeded: the same ``text``, ``seed`` and ``profile`` always produce
    the same output. Unknown profiles raise ``KeyError`` so a typo in a persona
    fails loudly instead of silently running clean. Disfluencies are applied
    first (how it was spoken), then ASR errors (how it was heard) — matching the
    real pipeline order mouth → microphone → transcript.
    """
    spec = _PROFILES[profile]
    rng = random.Random(seed)
    disfluency = spec["disfluency"]
    asr = spec["asr"]
    rate = spec["rate"]
    assert isinstance(disfluency, set) and isinstance(asr, set) and isinstance(rate, float)
    spoken = inject_disfluencies(text, rng=rng, types=disfluency, rate=rate)
    return inject_asr_errors(spoken, rng=rng, modes=asr)


def available_profiles() -> tuple[str, ...]:
    """Names accepted by :func:`garble` (for persona validation + CLI help)."""
    return tuple(_PROFILES)
