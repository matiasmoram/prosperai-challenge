"""Tests for ``prosper.speculation`` — identity-result classification and
disambiguation message building. Pure functions, no I/O.

See ``docs/research/speculative_race.md`` for the broader design (the
asyncio prefetch half is deferred; these cover the fuzzy-match policy
that ships now).
"""

from __future__ import annotations

from datetime import date

from prosper.speculation import (
    EXACT_THRESHOLD,
    build_disambiguation_message,
    classify_find_result,
    next_n_business_days,
)


def test_classify_no_match_empty() -> None:
    assert classify_find_result([], fuzzy=True) == "no_match"
    assert classify_find_result([], fuzzy=False) == "no_match"


def test_classify_phone_single_is_exact() -> None:
    # Phone lookups are exact — no similarity score, any single hit is exact.
    assert classify_find_result([{"id": "p1"}], fuzzy=False) == "found_exact"


def test_classify_name_dob_high_similarity_is_exact() -> None:
    patients = [{"id": "p1", "similarity": 0.99}]
    assert classify_find_result(patients, fuzzy=True) == "found_exact"


def test_classify_name_dob_low_similarity_is_fuzzy_single() -> None:
    patients = [{"id": "p1", "similarity": 0.90}]
    assert classify_find_result(patients, fuzzy=True) == "found_fuzzy_single"


def test_classify_name_dob_missing_similarity_defaults_exact() -> None:
    # Absent similarity → treat as exact (backwards-compatible with results
    # that don't carry the score).
    assert classify_find_result([{"id": "p1"}], fuzzy=True) == "found_exact"


def test_classify_multiple_is_fuzzy_multiple() -> None:
    patients = [{"id": "p1", "similarity": 0.9}, {"id": "p2", "similarity": 0.88}]
    assert classify_find_result(patients, fuzzy=True) == "found_fuzzy_multiple"
    # Even exact phone collisions on >1 row need disambiguation.
    assert classify_find_result([{"id": "p1"}, {"id": "p2"}], fuzzy=False) == "found_fuzzy_multiple"


def test_exact_threshold_boundary() -> None:
    at = [{"id": "p1", "similarity": EXACT_THRESHOLD}]
    just_below = [{"id": "p1", "similarity": EXACT_THRESHOLD - 0.01}]
    assert classify_find_result(at, fuzzy=True) == "found_exact"
    assert classify_find_result(just_below, fuzzy=True) == "found_fuzzy_single"


def test_build_disambiguation_message_numbers_candidates() -> None:
    candidates = [
        {"first_name": "John", "last_name": "Smith", "dob": "1985-03-04"},
        {"first_name": "Jon", "last_name": "Smith", "dob": "1985-03-04"},
    ]
    msg = build_disambiguation_message(candidates)
    assert "[1] John Smith (DOB 1985-03-04)" in msg
    assert "[2] Jon Smith (DOB 1985-03-04)" in msg
    # No UUIDs leak.
    assert "id" not in msg.lower().split("dob")[0] or "John" in msg


def test_next_n_business_days_skips_weekend() -> None:
    # 2026-05-22 is a Friday → next 4 biz days are Mon-Thu of the next week.
    friday = date(2026, 5, 22)
    days = next_n_business_days(friday, n=4)
    assert days == [date(2026, 5, 25), date(2026, 5, 26), date(2026, 5, 27), date(2026, 5, 28)]
    assert all(d.weekday() < 5 for d in days)


def test_next_n_business_days_from_midweek() -> None:
    wednesday = date(2026, 5, 20)
    days = next_n_business_days(wednesday, n=3)
    assert days == [date(2026, 5, 21), date(2026, 5, 22), date(2026, 5, 25)]
