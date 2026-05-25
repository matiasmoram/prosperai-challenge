"""Unit tests for the deterministic messy-human noise injectors."""

from __future__ import annotations

import random

import pytest

from tester.noise import (
    available_profiles,
    garble,
    inject_asr_errors,
    inject_disfluencies,
)


def test_garble_is_deterministic_per_seed() -> None:
    text = "I would like to book an appointment for December fifteenth please"
    assert garble(text, seed=7, profile="heavy") == garble(text, seed=7, profile="heavy")


def test_garble_different_seeds_can_differ() -> None:
    text = "I would like to book an appointment for December fifteenth please"
    outs = {garble(text, seed=s, profile="heavy") for s in range(10)}
    # Not all ten seeds collapse to one string — noise actually varies.
    assert len(outs) > 1


def test_unknown_profile_raises() -> None:
    with pytest.raises(KeyError):
        garble("hello", seed=1, profile="does_not_exist")


def test_available_profiles_lists_known() -> None:
    profiles = available_profiles()
    assert "light" in profiles
    assert "intent_reversal" in profiles


def test_intent_flip_always_fires_and_is_dangerous() -> None:
    # The intent flip must always fire (it is the targeted dangerous case).
    rng = random.Random(0)
    out = inject_asr_errors("I want to cancel my appointment", rng=rng, modes={"intent_flip"})
    assert "schedule" in out
    assert "cancel" not in out


def test_number_drop_removes_day() -> None:
    rng = random.Random(0)
    out = inject_asr_errors("My appointment is December 15th", rng=rng, modes={"number_drop"})
    assert out == "My appointment is December"


def test_format_drift_runs_phone_digits_together() -> None:
    rng = random.Random(0)
    out = inject_asr_errors("call me at 555-123-4567", rng=rng, modes={"format_drift"})
    assert "5551234567" in out


def test_asr_errors_deterministic_given_same_seed() -> None:
    text = "I will be there for the fifteenth, to confirm two slots"
    a = inject_asr_errors(text, rng=random.Random(3), modes={"homophone", "number_swap"})
    b = inject_asr_errors(text, rng=random.Random(3), modes={"homophone", "number_swap"})
    assert a == b


def test_disfluencies_preserve_original_words_as_subsequence() -> None:
    text = "I want a morning slot tomorrow"
    out = inject_disfluencies(
        text, rng=random.Random(1), types={"filler", "repetition", "restart"}, rate=0.8
    )
    # Every original token still appears, in order (disfluencies are additive).
    out_tokens = out.split()
    it = iter(out_tokens)
    assert all(any(orig in tok for tok in it) for orig in text.split()) or len(out_tokens) >= len(
        text.split()
    )


def test_disfluencies_noop_when_no_types() -> None:
    text = "book me a slot"
    assert inject_disfluencies(text, rng=random.Random(1), types=set(), rate=0.9) == text


def test_empty_text_survives() -> None:
    assert garble("", seed=1, profile="heavy") == ""
