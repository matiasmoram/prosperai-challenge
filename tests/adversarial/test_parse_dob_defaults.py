"""Adversarial: `_parse_dob` must not silently complete partial dates.

Finding F-001. `dateutil.parser.parse(raw, fuzzy=False)` still fills any
missing year/month/day component from `datetime.now()`. The `_parse_dob`
docstring promises ambiguous input "should fail loudly so the LLM re-asks
the user" — these tests prove it does the opposite, turning "May" into
today's date and registering corrupt DOBs into the EHR.

The xfail-marked tests assert the *correct* behaviour (an `Err`); they
flip to XPASS the day `_parse_dob` is fixed to reject default-injected
parses, forcing removal of the marker.
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
@pytest.mark.xfail(
    strict=True,
    reason="F-001: dateutil default-injects today's components; partial dates "
    "must be rejected but currently parse to an Ok with a fabricated date.",
)
def test_partial_date_must_be_rejected(raw: str) -> None:
    """A date string missing month/day/year should be an Err, not a guess."""
    result = _parse_dob(raw)
    assert is_err(result), f"{raw!r} parsed to {getattr(result, 'value', None)!r}"


def test_partial_date_currently_returns_today_components() -> None:
    """Documents the actual (buggy) behaviour so the regression is unambiguous.

    This is the mirror of the xfail above: it asserts the *current* wrong
    behaviour, so if the fix lands this test must be updated in lockstep with
    removing the xfail marker. Kept as a plain (passing) test on purpose.
    """
    result = _parse_dob("3pm")
    assert is_ok(result)
    # "3pm" carries no date whatsoever, yet we get a concrete calendar date.
    assert isinstance(result.value, date)


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
