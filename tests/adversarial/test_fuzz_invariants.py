"""Adversarial: seeded fuzz over the pure string-mangling functions.

No external fuzzing dependency — a fixed-seed `random.Random` drives a few
thousand structured-random strings through the redaction / normalisation
helpers and asserts the invariants their docstrings (and the event validator)
rely on. Currently all hold across 20k inputs; this committed run uses a
smaller, deterministic sample so CI stays fast and reproducible.

Invariants checked:
  - `redact_pii` is idempotent and never raises (docstring claim).
  - `mask_phone` output never contains a 7+ digit run, so it always passes
    `events._UNMASKED_DIGIT_RUN` (the bus PII safety net).
  - `normalize_phone` is idempotent (re-normalising a normalised number is a
    no-op — the API layer relies on this for its 409 uniqueness guard).
  - `normalize_name` never raises on arbitrary unicode.
"""

from __future__ import annotations

import random
import re

from prosper.ehr.repository import normalize_name, normalize_phone
from prosper.observability.redact import mask_phone, redact_pii

_DIGIT_RUN = re.compile(r"\d{7,}")
# Chars chosen to exercise phone (+-().digits), DOB (/-digits), email (@.),
# UUID/hex (a-f), unicode and whitespace branches.
_ALPHABET = "0123456789 +-().@/abcdefABCDEF xXzZ:;,áé東"
_ITERATIONS = 3000
_SEED = 20260523


def _rng(seed: int) -> random.Random:
    # Deterministic test fuzzer seed, not a security context.
    return random.Random(seed)  # noqa: S311


def _rand_str(rng: random.Random, maxlen: int = 40) -> str:
    return "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, maxlen)))


def test_redact_pii_is_idempotent_and_total() -> None:
    rng = _rng(_SEED)
    for _ in range(_ITERATIONS):
        s = _rand_str(rng)
        once = redact_pii(s)  # must not raise
        assert redact_pii(once) == once, f"not idempotent on {s!r}: {once!r}"


def test_mask_phone_never_emits_a_7_digit_run() -> None:
    rng = _rng(_SEED + 1)
    for _ in range(_ITERATIONS):
        s = _rand_str(rng)
        masked = mask_phone(s)
        assert not _DIGIT_RUN.search(masked), f"mask_phone({s!r}) leaked digits: {masked!r}"


def test_normalize_phone_is_idempotent() -> None:
    rng = _rng(_SEED + 2)
    for _ in range(_ITERATIONS):
        s = _rand_str(rng)
        first = normalize_phone(s)
        assert normalize_phone(first) == first, f"not idempotent on {s!r}: {first!r}"


def test_normalize_name_is_total() -> None:
    rng = _rng(_SEED + 3)
    for _ in range(_ITERATIONS):
        s = _rand_str(rng)
        normalize_name(s)  # must not raise on arbitrary unicode
