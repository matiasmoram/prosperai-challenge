"""Adversarial: PII-redaction gaps in `observability/redact.py`.

Findings F-003 (phone redaction misses) and F-004 (mask_name single-letter).
"""

from __future__ import annotations

import pytest

from prosper.console.events import ConsoleEvent, validate_event
from prosper.observability.redact import mask_name, redact_pii


def test_ten_digit_phone_is_redacted() -> None:
    """Baseline: the common case works — keeps the suite honest."""
    assert "[PHONE]" in redact_pii("phone 202 555 0142")


@pytest.mark.xfail(
    strict=True,
    reason="F-003: a phone run glued to a word ending in a hex letter (a-f) "
    "escapes redaction because of the UUID-avoidance lookbehind.",
)
def test_hex_glued_phone_is_redacted() -> None:
    """A 10-digit number immediately after 'ref' (ends in hex 'f') must mask."""
    out = redact_pii("ref2025550142 today")
    assert "[PHONE]" in out, f"phone leaked: {out!r}"


def test_seven_digit_local_number_is_not_redacted_documented_limitation() -> None:
    """Documents the deliberate 10-digit floor (module docstring).

    A US local 7-digit number ("555-0142") is below the floor and is NOT
    masked. This is a known PII gap for area-code-less numbers; pinned here
    so anyone tightening redaction sees the trade-off spelled out.
    """
    assert "[PHONE]" not in redact_pii("call me at 555-0142")


@pytest.mark.xfail(
    strict=True,
    reason="F-004: all-single-letter names produce no mask character, which "
    "the event validator then rejects, silently dropping the "
    "patient_identified telemetry event.",
)
def test_all_single_letter_name_still_masks() -> None:
    """`mask_name` should always emit at least one mask character."""
    masked = mask_name("A B")
    assert any(ch == "*" for ch in masked), f"no mask char in {masked!r}"


def test_single_letter_name_breaks_event_validation() -> None:
    """End-to-end consequence of F-004: the masked name fails event validation.

    Plain (passing) test documenting the downstream breakage — a
    `patient_identified` event built from a two-single-letter name raises in
    `validate_event`, so `Dispatcher._publish` drops it.
    """
    bad = ConsoleEvent(
        type="patient_identified",
        ts=0.0,
        session_id="s",
        payload={
            "name_masked": mask_name("A B"),  # → "A B", no mask char
            "dob_year": 1990,
            "phone_masked": "+1***0142",
            "id_internal": "uuid",
        },
    )
    with pytest.raises(ValueError, match="no masking character"):
        validate_event(bad)
