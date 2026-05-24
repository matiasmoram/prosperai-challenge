"""Adversarial: `_phone_words_to_digits` corrupts phones via the "for"→4 map.

Finding F-002. `_NUMBER_WORDS` maps "for" → "4" to recover the STT slip
"five-for-six" → 456. But "for" is also a ubiquitous English filler; when it
lands next to a literal digit run it splices a spurious 4 into an otherwise
valid phone number.
"""

from __future__ import annotations

from prosper.tools import _phone_words_to_digits


def test_for_filler_does_not_corrupt_a_real_phone() -> None:
    """`for` as filler before a complete number must not add a 4 (F-002 fixed)."""
    out = _phone_words_to_digits("for 5551234567")
    assert out == "5551234567", f"got {out!r} (spurious 4 injected)"


def test_for_filler_mid_number_does_not_corrupt() -> None:
    """`for` spliced between digit groups must not corrupt the number (F-002 fixed)."""
    out = _phone_words_to_digits("555 for 1234567")
    assert out == "5551234567", f"got {out!r}"


def test_genuine_spoken_digits_still_normalise() -> None:
    """The intended use — fully spoken digits — must keep working after a fix."""
    out = _phone_words_to_digits("five five five one two three four five six seven")
    assert out == "5551234567"


def test_five_for_six_slip_still_recovers() -> None:
    """Documents the original motivating case: 'five for six' → 5-4-6.

    The "for"→4 alias exists to recover an STT slip of "four" → "for", so
    "five for six …" must normalise to 5,4,6,… ("546…"), surrounded by
    number-words (not literal digits). Any fix for F-002 should keep this
    working. Plain test — currently passes.
    """
    out = _phone_words_to_digits("five for six seven eight nine zero one two three")
    assert out == "5467890123"


def test_non_string_passthrough() -> None:
    """Defensive: a non-string arg is returned untouched, never crashes."""
    assert _phone_words_to_digits(None) is None  # type: ignore[arg-type]
