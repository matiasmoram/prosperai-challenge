"""Adversarial: `_parse_dob` must not silently complete partial dates.

Finding F-001. `dateutil.parser.parse(raw, fuzzy=False)` still fills any
missing year/month/day component from `datetime.now()`. The `_parse_dob`
docstring promises ambiguous input "should fail loudly so the LLM re-asks
the user" — these tests prove it does the opposite, turning "May" into
today's date and registering corrupt DOBs into the EHR.

FIXED: `_parse_dob` now parses with two differing sentinel defaults and
rejects the input when the two parses disagree on year/month/day (i.e. a
component was absent and default-injected). These tests now pass as plain
assertions of the corrected behaviour.
"""

from __future__ import annotations

from datetime import date

import pytest

from prosper.result import is_err, is_ok
from prosper.tools import _parse_dob


@pytest.mark.parametrize(
    "raw",
    [
        "March",  # month name only → today's day-of-month injected
        "May",
        "3pm",  # no date at all → today
        "15",  # day only → today's month + year
    ],
)
def test_partial_date_must_be_rejected(raw: str) -> None:
    """A date string missing month/day/year is an Err, not a guess (F-001 fixed)."""
    result = _parse_dob(raw)
    assert is_err(result), f"{raw!r} parsed to {getattr(result, 'value', None)!r}"


def test_time_only_input_is_rejected() -> None:
    """'3pm' carries no date at all → must be rejected, not resolved to today."""
    result = _parse_dob("3pm")
    assert is_err(result)
    assert result.code == "dob_unparseable"


def test_full_iso_date_still_parses() -> None:
    """Guard: the fix for F-001 must not break legitimate full dates."""
    result = _parse_dob("1992-04-03")
    assert is_ok(result)
    assert result.value == date(1992, 4, 3)


def test_written_full_date_still_parses() -> None:
    """A fully-specified written date is unambiguous and must survive any fix."""
    result = _parse_dob("April 3 1992")
    assert is_ok(result)
    assert result.value == date(1992, 4, 3)
