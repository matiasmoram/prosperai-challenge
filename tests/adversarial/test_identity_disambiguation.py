"""Adversarial: the name+DOB disambiguation picker must be crash-proof and
never select an out-of-range / unintended candidate (identity is a hard
checkpoint — picking the wrong row identifies the caller as someone else).

Covers `_pick_candidate_index` and the `_resolve_pending_identity` flow added
for fuzzy multi-candidate name+DOB matches.
"""

from __future__ import annotations

import random

from prosper.dispatcher import Dispatcher, _pick_candidate_index
from prosper.flows import State

_ALPHABET = "0123456789 abcfirstsecondthirdlastonetwothreefour#numberoption.,'-"


def _rng(seed: int) -> random.Random:
    # Deterministic test fuzzer seed, not a security context.
    return random.Random(seed)  # noqa: S311


def test_pick_index_never_crashes_and_stays_in_bounds() -> None:
    rng = _rng(7)
    for _ in range(5000):
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 24)))
        count = rng.randint(0, 4)
        idx = _pick_candidate_index(text, count)
        assert idx is None or (0 <= idx < count), f"{text!r}, count={count} -> {idx}"


def test_basic_picks_resolve() -> None:
    assert _pick_candidate_index("the first one", 3) == 0
    assert _pick_candidate_index("number two", 3) == 1
    assert _pick_candidate_index("2", 3) == 1
    assert _pick_candidate_index("the last one", 3) == 2
    assert _pick_candidate_index("two", 3) == 1


def test_out_of_range_pick_returns_none() -> None:
    """'the fifth' / '5' when only 2 candidates must NOT select anything."""
    assert _pick_candidate_index("the fifth one", 2) is None
    assert _pick_candidate_index("number 5", 2) is None
    assert _pick_candidate_index("option 9", 2) is None


def test_stray_dob_digits_do_not_select() -> None:
    """A 4-digit year or a 2-digit age the caller mentions must not be parsed
    as a candidate pick (only 1-2 digit handles in range count)."""
    assert _pick_candidate_index("my birth year is 1990", 2) is None
    assert _pick_candidate_index("i'm 47 years old", 2) is None  # 47 > count


def test_pronoun_one_does_not_misidentify() -> None:
    """F-013: a bare 'one' used as a PRONOUN must not select candidate #1.

    A caller who is unsure or asks to repeat ('one more time', 'not that one',
    'neither one') was silently identified as the first candidate and advanced
    under the wrong identity — an identity-checkpoint failure."""
    for text in (
        "can you repeat them one more time",
        "one more time please",
        "not that one",
        "which one was that",
        "the fifth one",
        "i'm not sure, neither one",
    ):
        assert _pick_candidate_index(text, 2) is None, f"{text!r} misfired"


def test_standalone_and_cued_cardinals_still_pick() -> None:
    """Genuine picks must keep working after the F-013 fix."""
    assert _pick_candidate_index("one", 2) == 0
    assert _pick_candidate_index("two", 2) == 1
    assert _pick_candidate_index("two please", 2) == 1
    assert _pick_candidate_index("number two", 2) == 1
    assert _pick_candidate_index("option one", 2) == 0


class _FakeLLM:
    async def generate(self, **_: object):  # pragma: no cover
        raise AssertionError("LLM must not be called")


class _FakeEHR:
    def set_session_id(self, _s: str) -> None: ...
    def set_turn_id(self, _t: int) -> None: ...


def _dispatcher_pending(candidates: list[dict]) -> Dispatcher:
    d = Dispatcher(llm=_FakeLLM(), ehr_client=_FakeEHR())  # type: ignore[arg-type]
    d.state = State.IDENTIFY_PATIENT
    d.memory.pending_identity_candidates = candidates
    return d


def test_valid_pick_identifies_and_advances() -> None:
    cands = [
        {"id": "p1", "first_name": "Jon", "last_name": "Smith", "dob": "1990-01-01"},
        {"id": "p2", "first_name": "John", "last_name": "Smith", "dob": "1990-01-01"},
    ]
    d = _dispatcher_pending(cands)
    d._maybe_transition_from_user_text("the second one")
    assert d.memory.identified_patient == cands[1]
    assert d.memory.pending_identity_candidates == []
    assert d.state is State.CHOOSE_INTENT


def test_unparseable_pick_stays_pending_unidentified() -> None:
    """If the pick can't be parsed, we must NOT guess an identity — stay put."""
    cands = [
        {"id": "p1", "first_name": "Jon", "last_name": "Smith", "dob": "1990-01-01"},
        {"id": "p2", "first_name": "John", "last_name": "Smith", "dob": "1990-01-01"},
    ]
    d = _dispatcher_pending(cands)
    d._maybe_transition_from_user_text("um, I'm not sure, can you repeat?")
    assert d.memory.identified_patient is None
    assert d.memory.pending_identity_candidates == cands
    assert d.state is State.IDENTIFY_PATIENT
